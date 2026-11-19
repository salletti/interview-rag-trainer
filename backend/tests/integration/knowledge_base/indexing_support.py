"""Aides des tests d'intégration S4 : Qdrant réel (``compose.test.yaml``,
``QDRANT_*``), PostgreSQL réel, embeddings par un ``Embedder`` de test
déterministe (``HashEmbedder``, aucune dépendance de modèle) OU par le vrai
modèle (``SentenceTransformersEmbedder``, marqueur ``model``).

Les tests tournent deux fois quand c'est utile : paramètre ``hash`` (toujours)
et paramètre ``model`` (ignoré tant que ``sentence_transformers`` n'est pas
installé : ``pytest.importorskip``). Marqueur à déclarer dans
``[tool.pytest.ini_options].markers`` : ``model``.

Tables figées par ces tests (schéma ``knowledge_base``, cf.
``test_indexing_migration``) : ``knowledge_corpus``, ``search_indexes``,
``indexations``.

Composition de production figée (en plus de celle de S3) :

- ``knowledge_base.adapters.gateway.vector_store.qdrant_vector_index.
  QdrantVectorIndex(client: AsyncQdrantClient)`` avec la méthode statique
  ``collection_name(index_id)`` ; implémente ``VectorIndex`` (voir
  ``tests/unit/knowledge_base/use_cases/indexing_fakes.py``). Payload d'un point :
  les noms de ``IndexedPoint`` (hors ``point_id`` et ``vector``) ; UUID en texte,
  ``topics`` liste, ``visible`` booléen.
- ``knowledge_base.adapters.controller.composition.
  build_provision_search_index_handler(session_factory, vector_index, *,
  id_factory=None, clock=None) -> ProvisionSearchIndexHandler`` (idempotent :
  appelé au démarrage de l'application, aucune route HTTP).
- ``knowledge_base.adapters.controller.tasks.index_version_handler.
  build_index_version_handler`` et ``...sync_visibility_handler.
  build_sync_visibility_handler`` (signatures : voir
  ``tests/unit/knowledge_base/test_indexing_work_handlers.py``).
- Le handler ``extract_version`` de S3 (``build_extract_version_handler``) crée
  désormais les ``Indexation`` et les items ``index_version``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import PointIdsList, Record
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_base.adapters.controller.composition import (
    build_provision_search_index_handler,
)
from knowledge_base.adapters.controller.tasks.execution import (
    WorkContext,
    WorkOutcome,
)
from knowledge_base.adapters.controller.tasks.index_version_handler import (
    IndexVersionWorkHandler,
    build_index_version_handler,
)
from knowledge_base.adapters.controller.tasks.registry import WorkHandler
from knowledge_base.adapters.controller.tasks.retry_policy import TransientWorkError
from knowledge_base.adapters.controller.tasks.sync_visibility_handler import (
    SyncVisibilityWorkHandler,
    build_sync_visibility_handler,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    ClaimedWorkItem,
)
from knowledge_base.adapters.gateway.vector_store.qdrant_vector_index import (
    QdrantVectorIndex,
)
from knowledge_base.domain.providers.embedder import Embedder
from knowledge_base.domain.providers.vector_index import VectorIndex
from knowledge_base.domain.services.point_id import point_id
from knowledge_base.domain.value_objects.embedding_profile import EmbeddingProfile
from knowledge_base.domain.value_objects.index_manifest import IndexManifest
from knowledge_base.domain.value_objects.indexed_point import (
    IndexedPoint,
    PointSelector,
)
from knowledge_base.use_cases.provision_search_index.provision_search_index_command import (  # noqa: E501
    ProvisionSearchIndex,
)
from knowledge_base.use_cases.provision_search_index.provision_search_index_handler import (  # noqa: E501
    ProvisionSearchIndexHandler,
)
from runtime.database import SessionFactory
from runtime.settings import Settings
from runtime.vector_store import create_qdrant_client
from tests.integration.knowledge_base.extraction_support import (
    FAST_RETRY,
    POLICY,
    ExtractionRig,
    Imported,
    open_extraction_rig,
)
from tests.integration.knowledge_base.support import kb_settings
from tests.unit.knowledge_base.use_cases.indexing_fakes import (
    HashEmbedder,
    single_profile,
)

EMBEDDER_PARAMS = [
    "hash",
    pytest.param("model", marks=[pytest.mark.model]),
]

QA_SHEET = (
    "# Fiche FastAPI\n\n"
    "## Question : Qu'est-ce que Depends ?\n\nDepends injecte une dépendance.\n\n"
    "## Question : Qu'est-ce que lifespan ?\n\nIl gère le cycle de vie.\n\n"
    "## Question : Qu'est-ce qu'un middleware ?\n\nIl enveloppe chaque requête.\n"
).encode()
QA_SHEET_V2 = (
    "# Fiche FastAPI\n\n"
    "## Question : Qu'est-ce que Depends ?\n\nDepends résout les dépendances.\n\n"
    "## Question : Qu'est-ce que lifespan ?\n\nIl gère démarrage et arrêt.\n"
).encode()


def make_embedder(kind: str) -> Embedder:
    if kind == "hash":
        return HashEmbedder()
    pytest.importorskip("sentence_transformers")
    from knowledge_base.adapters.gateway.embedding.sentence_transformers_embedder import (  # noqa: E501
        SentenceTransformersEmbedder,
    )
    from knowledge_base.domain.value_objects.embedding_profile import (
        MULTILINGUAL_E5_BASE,
    )

    return SentenceTransformersEmbedder.load(MULTILINGUAL_E5_BASE)


@dataclass(frozen=True)
class IndexingRig:
    extraction: ExtractionRig
    qdrant: AsyncQdrantClient
    vector: QdrantVectorIndex
    embedder: Embedder
    provisioner: ProvisionSearchIndexHandler
    index_handler: IndexVersionWorkHandler
    sync_handler: SyncVisibilityWorkHandler

    @property
    def profile(self) -> EmbeddingProfile:
        return self.embedder.profile

    def index_handler_over(self, vector: VectorIndex) -> IndexVersionWorkHandler:
        return build_index_version_handler(
            self.extraction.session_factory,
            self.extraction.settings,
            embedders=single_profile(self.embedder),
            vector_index=vector,
        )

    # --- scénario ---

    async def provision(self) -> UUID:
        return await self.provisioner.handle(
            ProvisionSearchIndex(profile=self.profile, chunking_policy=POLICY)
        )

    async def import_and_extract(
        self, content: bytes = QA_SHEET, *, kind: str = "QA_SHEET"
    ) -> Imported:
        imported = await self.extraction.import_document(content, kind=kind)
        assert await self.extraction.run(imported.item_id) == "succeeded"
        return imported

    async def import_version_and_extract(
        self, document_id: UUID, content: bytes
    ) -> Imported:
        response = await self.extraction.imports.post_version(document_id, content)
        assert response.status_code == 202, response.text
        body = response.json()
        (item,) = [
            i
            for i in await self.extraction.imports.extract_items()
            if i["payload"]["version_id"] == body["version_id"]
        ]
        imported = Imported(document_id, UUID(body["version_id"]), item["id"])
        assert await self.extraction.run(imported.item_id) == "succeeded"
        return imported

    async def index_item(self, version_id: UUID, index_id: UUID) -> UUID:
        rows = await self.extraction.imports.rows(
            "SELECT id, payload FROM knowledge_base.work_items "
            "WHERE kind = 'index_version'"
        )
        (row,) = [
            r
            for r in rows
            if r["payload"]["version_id"] == str(version_id)
            and r["payload"]["index_id"] == str(index_id)
        ]
        return cast(UUID, row["id"])

    async def run_index(
        self,
        version_id: UUID,
        index_id: UUID,
        handler: WorkHandler | None = None,
    ) -> WorkOutcome:
        item_id = await self.index_item(version_id, index_id)
        return await self.extraction.run(item_id, handler or self.index_handler)

    async def run_sync(self, document_id: UUID, revision: int) -> WorkOutcome:
        (row,) = await self.extraction.imports.rows(
            "SELECT id FROM knowledge_base.work_items WHERE dedup_key = :k",
            k=f"sync_visibility:{document_id}:{revision}",
        )
        return await self.extraction.run(row["id"], self.sync_handler)

    # --- lectures ---

    async def indexation(self, version_id: UUID, index_id: UUID) -> dict[str, Any]:
        (row,) = await self.extraction.imports.rows(
            "SELECT * FROM knowledge_base.indexations "
            "WHERE version_id = :v AND index_id = :i",
            v=version_id,
            i=index_id,
        )
        return row

    async def document(self, document_id: UUID) -> dict[str, Any]:
        (row,) = await self.extraction.imports.rows(
            "SELECT * FROM knowledge_base.documents WHERE id = :id", id=document_id
        )
        return row

    async def points(self, index_id: UUID) -> list[Record]:
        records, _ = await self.qdrant.scroll(
            collection_name=QdrantVectorIndex.collection_name(index_id),
            limit=10_000,
            with_payload=True,
            with_vectors=False,
        )
        return records

    async def expected_manifest(
        self, version_id: UUID, index_id: UUID
    ) -> IndexManifest:
        """Manifest calculé hors Qdrant, depuis les chunks de PostgreSQL."""
        chunks = await self.extraction.chunks(version_id)
        return IndexManifest.of(
            (point_id(index_id, c["id"]), c["chunk_hash"]) for c in chunks
        )

    async def point_ids(self, index_id: UUID) -> set[UUID]:
        return {UUID(str(r.id)) for r in await self.points(index_id)}


def payload(record: Record) -> dict[str, Any]:
    assert record.payload is not None
    return record.payload


async def clean_indexing(
    session_factory: SessionFactory, qdrant: AsyncQdrantClient
) -> None:
    """Vide les tables de S4 puis supprime les collections de ces index
    (``DELETE`` des indexations AVANT les versions : clés étrangères)."""
    async with session_factory() as session, session.begin():
        found = await session.execute(
            text("SELECT index_id FROM knowledge_base.search_indexes")
        )
        index_ids = [row[0] for row in found]
        await session.execute(text("DELETE FROM knowledge_base.indexations"))
        await session.execute(text("DELETE FROM knowledge_base.search_indexes"))
        await session.execute(text("DELETE FROM knowledge_base.knowledge_corpus"))
    for index_id in index_ids:
        with suppress(Exception):
            await qdrant.delete_collection(QdrantVectorIndex.collection_name(index_id))


@asynccontextmanager
async def open_indexing_rig(
    settings: Settings,
    session_factory: SessionFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    embedder: Embedder,
) -> AsyncIterator[IndexingRig]:
    qdrant = create_qdrant_client(settings)
    try:
        await clean_indexing(session_factory, qdrant)
        vector = QdrantVectorIndex(qdrant)
        kb = kb_settings(**FAST_RETRY)
        async with open_extraction_rig(
            settings, session_factory, tmp_path, monkeypatch
        ) as extraction:
            try:
                yield IndexingRig(
                    extraction=extraction,
                    qdrant=qdrant,
                    vector=vector,
                    embedder=embedder,
                    provisioner=build_provision_search_index_handler(
                        session_factory, vector
                    ),
                    index_handler=build_index_version_handler(
                        session_factory,
                        kb,
                        embedders=single_profile(embedder),
                        vector_index=vector,
                    ),
                    sync_handler=build_sync_visibility_handler(
                        session_factory, vector_index=vector
                    ),
                )
            finally:
                await clean_indexing(session_factory, qdrant)
    finally:
        await qdrant.close()


# --- pannes injectées ---------------------------------------------------------------


class CrashBeforeFinalize:
    """Simule la mort du worker après les effets Qdrant, avant la finalisation
    (le travail hors transaction a eu lieu, la transaction de finalisation non)."""

    def __init__(self, inner: WorkHandler) -> None:
        self._inner = inner
        self.kind = inner.kind
        self.queue = inner.queue
        self.max_attempts = inner.max_attempts

    async def execute(self, item: ClaimedWorkItem, ctx: WorkContext) -> None:
        class Dying:
            heartbeat_interval = timedelta(seconds=30)

            async def heartbeat(self) -> None:
                return None

            async def finalize(
                self, callback: Callable[[AsyncSession], Awaitable[None]]
            ) -> None:
                raise TransientWorkError("worker interrompu avant la finalisation")

        await self._inner.execute(item, cast(WorkContext, Dying()))


Tamper = Callable[[AsyncQdrantClient, UUID, Sequence[IndexedPoint]], Awaitable[None]]


async def delete_one_point(
    qdrant: AsyncQdrantClient, index_id: UUID, points: Sequence[IndexedPoint]
) -> None:
    await qdrant.delete(
        collection_name=QdrantVectorIndex.collection_name(index_id),
        points_selector=PointIdsList(points=[str(points[0].point_id)]),
        wait=True,
    )


async def corrupt_one_hash(
    qdrant: AsyncQdrantClient, index_id: UUID, points: Sequence[IndexedPoint]
) -> None:
    await qdrant.set_payload(
        collection_name=QdrantVectorIndex.collection_name(index_id),
        payload={"chunk_hash": "0" * 64},
        points=[str(points[0].point_id)],
        wait=True,
    )


class TamperingVectorIndex:
    """Délègue à l'adapter réel et altère Qdrant UNE fois, juste après le premier
    upsert : l'état que verrait la vérification si Qdrant perdait ou modifiait un
    point entre l'écriture et la relecture."""

    def __init__(
        self, inner: QdrantVectorIndex, qdrant: AsyncQdrantClient, tamper: Tamper
    ):
        self._inner = inner
        self._qdrant = qdrant
        self._tamper = tamper
        self.tampered = False

    async def ensure_collection(
        self, index_id: UUID, profile: EmbeddingProfile
    ) -> None:
        await self._inner.ensure_collection(index_id, profile)

    async def check_collection(self, index_id: UUID, profile: EmbeddingProfile) -> bool:
        return await self._inner.check_collection(index_id, profile)

    async def upsert(
        self, index_id: UUID, points: Sequence[IndexedPoint], *, wait: bool = True
    ) -> None:
        await self._inner.upsert(index_id, points, wait=wait)
        if not self.tampered:
            self.tampered = True
            await self._tamper(self._qdrant, index_id, points)

    async def retrieve(
        self, index_id: UUID, point_ids: Sequence[UUID]
    ) -> Sequence[tuple[UUID, str]]:
        return await self._inner.retrieve(index_id, point_ids)

    async def set_visibility(
        self, index_id: UUID, *, document_id: UUID, version_id: UUID, visible: bool
    ) -> None:
        await self._inner.set_visibility(
            index_id, document_id=document_id, version_id=version_id, visible=visible
        )

    async def delete_points(self, index_id: UUID, selector: PointSelector) -> None:
        await self._inner.delete_points(index_id, selector)


async def wait_for_backoff() -> None:
    await asyncio.sleep(0.05)  # backoff de 10 ms (réglage de test)
