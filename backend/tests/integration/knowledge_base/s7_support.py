"""Aides des tests d'intégration S7 (versions concurrentes, retrait, purge) :
PostgreSQL, Qdrant et work queue RÉELS, embeddings par ``HashEmbedder`` (aucune
dépendance de modèle : ces tests vérifient des invariants de visibilité, pas la
qualité du classement), volume de blobs temporaire.

Concurrence : jamais de ``sleep``. Les entrelacements sont imposés par des
barrières explicites (``asyncio.Event``, ``asyncio.Barrier``) posées sur les
frontières (``GatedVectorIndex`` pour Qdrant, ``CoordinatedUnitOfWork`` pour
PostgreSQL, ``InterferingHandler`` pour la finalisation d'un work item).

Composition figée par S7 (en plus de celles de S2 à S5) :

- ``DELETE /api/v1/knowledge-base/documents/{id}`` (voir
  ``test_withdraw_document_routes_http``) ;
- ``knowledge_base.adapters.controller.tasks.purge_document_handler.
  build_purge_document_handler(session_factory, settings, *, vector_index)`` ;
- ``knowledge_base.adapters.controller.composition.
  build_clean_blob_orphans_handler(session_factory, settings)`` : le cas
  d'usage ``CleanBlobOrphansHandler`` sur le volume ``settings.blob_root`` et
  les versions de PostgreSQL ;
- colonne ``knowledge_base.documents.purged_at`` (``timestamptz``, nulle tant
  que le document n'est pas purgé), migration Alembic de S7.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from httpx import AsyncClient, Response
from qdrant_client.models import Record
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_base.adapters.controller.composition import compose_knowledge_search
from knowledge_base.adapters.controller.tasks.execution import (
    WorkContext,
    WorkOutcome,
)
from knowledge_base.adapters.controller.tasks.registry import WorkHandler
from knowledge_base.adapters.gateway.persistence.sqlalchemy_knowledge_document_repository import (  # noqa: E501
    SqlAlchemyKnowledgeDocumentRepository,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    ClaimedWorkItem,
)
from knowledge_base.adapters.gateway.vector_store.qdrant_vector_index import (
    QdrantVectorIndex,
)
from knowledge_base.contracts.providers import KnowledgeSearch
from knowledge_base.contracts.result import KnowledgeQuery, KnowledgeSearchResult
from knowledge_base.domain.entities.knowledge_document import KnowledgeDocument
from knowledge_base.domain.providers.knowledge_document_repository import ContentOwner
from knowledge_base.domain.providers.work_queue import FailureInfo
from knowledge_base.domain.value_objects.content_hash import ContentHash
from knowledge_base.domain.value_objects.embedding_profile import EmbeddingProfile
from knowledge_base.domain.value_objects.indexed_point import (
    IndexedPoint,
    PointSelector,
)
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.extraction_support import Imported
from tests.integration.knowledge_base.indexing_support import (
    QA_SHEET,
    IndexingRig,
    open_indexing_rig,
    payload,
)
from tests.integration.knowledge_base.support import kb_settings
from tests.unit.knowledge_base.domain.services.support import COUNTER
from tests.unit.knowledge_base.use_cases.indexing_fakes import HashEmbedder

PREFIX = "/api/v1/knowledge-base"
PYDANTIC_SHEET = QA_SHEET.replace(b"FastAPI", b"Pydantic")
QUERY = "Qu'est-ce que Depends ?"
TIMEOUT = 20.0
GRACE = timedelta(hours=24)


def age(path: Path, by: timedelta) -> None:
    """Date de modification reculée de ``by`` (le fichier date de dépôt) : le
    délai de grâce se teste sans attendre."""
    moment = (datetime.now(UTC) - by).timestamp()
    os.utime(path, (moment, moment))


def query(limit: int = 20) -> KnowledgeQuery:
    return KnowledgeQuery(text=QUERY, limit=limit)


def versions_of(result: KnowledgeSearchResult, document_id: UUID) -> set[UUID]:
    return {
        p.citation.version_id
        for p in result.passages
        if p.citation.document_id == document_id
    }


def documents_of(result: KnowledgeSearchResult) -> set[UUID]:
    return {p.citation.document_id for p in result.passages}


# --- barrières sur Qdrant ------------------------------------------------------------


class GatedVectorIndex:
    """Délègue à l'adapter réel ; le PREMIER appel de ``gate_on`` signale
    ``reached`` puis attend ``release`` avant d'agir : le test fait ce qu'il
    veut dans cette fenêtre, sans délai."""

    def __init__(self, inner: QdrantVectorIndex, *, gate_on: str) -> None:
        self._inner = inner
        self._gate_on = gate_on
        self._passed = False
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def _gate(self, operation: str) -> None:
        if operation == self._gate_on and not self._passed:
            self._passed = True
            self.reached.set()
            async with asyncio.timeout(TIMEOUT):
                await self.release.wait()

    async def ensure_collection(
        self, index_id: UUID, profile: EmbeddingProfile
    ) -> None:
        await self._inner.ensure_collection(index_id, profile)

    async def check_collection(self, index_id: UUID, profile: EmbeddingProfile) -> bool:
        return await self._inner.check_collection(index_id, profile)

    async def upsert(
        self, index_id: UUID, points: Sequence[IndexedPoint], *, wait: bool = True
    ) -> None:
        await self._gate("upsert")
        await self._inner.upsert(index_id, points, wait=wait)

    async def retrieve(
        self, index_id: UUID, point_ids: Sequence[UUID]
    ) -> Sequence[tuple[UUID, str]]:
        return await self._inner.retrieve(index_id, point_ids)

    async def set_visibility(
        self, index_id: UUID, *, document_id: UUID, version_id: UUID, visible: bool
    ) -> None:
        await self._gate("set_visibility")
        await self._inner.set_visibility(
            index_id, document_id=document_id, version_id=version_id, visible=visible
        )

    async def delete_points(self, index_id: UUID, selector: PointSelector) -> None:
        await self._gate("delete_points")
        await self._inner.delete_points(index_id, selector)


class UnreachableVectorIndex(GatedVectorIndex):
    """Qdrant dont la suppression de points échoue (réseau coupé)."""

    def __init__(self, inner: QdrantVectorIndex) -> None:
        super().__init__(inner, gate_on="never")

    async def delete_points(self, index_id: UUID, selector: PointSelector) -> None:
        raise ConnectionError("Qdrant injoignable")


# --- barrière sur la finalisation d'un work item -------------------------------------


class _BeforeFinalize:
    def __init__(
        self, inner: WorkContext, before: Callable[[], Awaitable[None]]
    ) -> None:
        self._inner = inner
        self._before = before

    @property
    def finalized(self) -> bool:
        return self._inner.finalized

    @property
    def heartbeat_interval(self) -> Any:
        return self._inner.heartbeat_interval

    async def heartbeat(self) -> None:
        await self._inner.heartbeat()

    async def finalize(
        self, callback: Callable[[AsyncSession], Awaitable[None]]
    ) -> None:
        await self._before()
        await self._inner.finalize(callback)


class InterferingHandler:
    """Enveloppe le vrai handler : mêmes ``kind``, file, tentatives et hook
    d'échec ; ``before`` s'exécute juste avant ``ctx.finalize``, quand le travail
    hors transaction est terminé."""

    def __init__(self, inner: Any, before: Callable[[], Awaitable[None]]) -> None:
        self._inner = inner
        self._before = before
        self.kind = inner.kind
        self.queue = inner.queue
        self.max_attempts = inner.max_attempts

    async def execute(self, item: ClaimedWorkItem, ctx: WorkContext) -> None:
        await self._inner.execute(
            item, cast(WorkContext, _BeforeFinalize(ctx, self._before))
        )

    async def on_terminal_failure(
        self, session: AsyncSession, item: ClaimedWorkItem, error: FailureInfo
    ) -> None:
        await self._inner.on_terminal_failure(session, item, error)

    def as_work_handler(self) -> WorkHandler:
        return cast(WorkHandler, self)


# --- barrière sur PostgreSQL : import et retrait concurrents -------------------------


class Coordination:
    """Les deux transactions lisent le document AVANT que l'une n'écrive ;
    celle qui ne gagne pas attend le commit du gagnant avant d'écrire : le
    verrou optimiste est perdu à coup sûr, dans l'ordre voulu."""

    def __init__(self, winner: str) -> None:
        self.winner = winner
        self.both_read = asyncio.Barrier(2)
        self.winner_committed = asyncio.Event()
        self.attempts: dict[str, int] = {}


class _GatedDocuments:
    def __init__(
        self,
        inner: SqlAlchemyKnowledgeDocumentRepository,
        role: str,
        coordination: Coordination,
        first_attempt: bool,
    ) -> None:
        self._inner = inner
        self._role = role
        self._coordination = coordination
        self._first_attempt = first_attempt

    async def get(self, document_id: UUID) -> KnowledgeDocument | None:
        found = await self._inner.get(document_id)
        if self._first_attempt:
            async with asyncio.timeout(TIMEOUT):
                await self._coordination.both_read.wait()
                if self._role != self._coordination.winner:
                    await self._coordination.winner_committed.wait()
        return found

    async def find_by_content_hash(
        self, content_hash: ContentHash
    ) -> ContentOwner | None:
        return await self._inner.find_by_content_hash(content_hash)

    async def add(self, document: KnowledgeDocument) -> None:
        await self._inner.add(document)

    async def save(self, document: KnowledgeDocument) -> None:
        await self._inner.save(document)


class CoordinatedUnitOfWork(SqlAlchemyUnitOfWork):
    def __init__(
        self, session_factory: SessionFactory, role: str, coordination: Coordination
    ) -> None:
        super().__init__(session_factory)
        attempt = coordination.attempts.get(role, 0) + 1
        coordination.attempts[role] = attempt
        self._role = role
        self._coordination = coordination
        self._gated = _GatedDocuments(self._documents, role, coordination, attempt == 1)

    @property
    def documents(self) -> _GatedDocuments:  # type: ignore[override]
        return self._gated

    async def commit(self) -> None:
        await super().commit()
        if self._role == self._coordination.winner:
            self._coordination.winner_committed.set()


# --- tâches de fond ------------------------------------------------------------------


class Background:
    """Tâches lancées par un test ; annulées et attendues à la fin du test,
    même s'il échoue au milieu : une tâche qui survivrait chercherait dans un
    corpus que la fixture suivante est en train de vider."""

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task[Any]] = []

    def spawn(self, coroutine: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine)
        self._tasks.append(task)
        return task

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


@asynccontextmanager
async def background_tasks() -> AsyncIterator[Background]:
    tasks = Background()
    try:
        yield tasks
    finally:
        await tasks.close()


# --- harnais -------------------------------------------------------------------------


@dataclass(frozen=True)
class S7Rig:
    indexing: IndexingRig
    index_id: UUID
    search: KnowledgeSearch

    @property
    def http(self) -> AsyncClient:
        return self.indexing.extraction.imports.client

    @property
    def session_factory(self) -> SessionFactory:
        return self.indexing.extraction.session_factory

    @property
    def blob_root(self) -> Path:
        return self.indexing.extraction.imports.blob_root

    # --- scénario ---

    async def import_extract(
        self, content: bytes = QA_SHEET, *, title: str = "Fiche"
    ) -> Imported:
        """Document importé par l'API puis extrait : version ``EXTRACTED``,
        indexation et item ``index_version`` ``PENDING``."""
        imports = self.indexing.extraction.imports
        response = await imports.post_document(
            content, kind="QA_SHEET", title=title, filename="fiche.md"
        )
        assert response.status_code == 202, response.text
        imported = await self._imported(response)
        await self.extract(imported)
        return imported

    async def publish(
        self, content: bytes = QA_SHEET, *, title: str = "Fiche"
    ) -> Imported:
        """Importé, extrait, indexé, publié et synchronisé : visible à la
        recherche."""
        imported = await self.import_extract(content, title=title)
        assert await self.indexing.run_index(imported.version_id, self.index_id) == (
            "succeeded"
        )
        await self.synchronize(imported.document_id)
        return imported

    async def post_version(self, document_id: UUID, content: bytes) -> Imported:
        """Nouvelle version importée par l'API (pas encore extraite)."""
        response = await self.indexing.extraction.imports.post_version(
            document_id, content
        )
        assert response.status_code == 202, response.text
        return await self._imported(response, document_id)

    async def extract(self, imported: Imported) -> None:
        assert await self.indexing.extraction.run(imported.item_id) == "succeeded"

    async def synchronize(self, document_id: UUID) -> None:
        document = await self.indexing.document(document_id)
        item_id = await self.item_id(
            f"sync_visibility:{document_id}:{document['visibility_revision']}"
        )
        assert await self.indexing.extraction.run(
            item_id, self.indexing.sync_handler
        ) == ("succeeded")

    async def withdraw(
        self, document_id: UUID, *, expect: int | None = 202
    ) -> Response:
        response = await self.http.delete(f"{PREFIX}/documents/{document_id}")
        if expect is not None:
            assert response.status_code == expect, response.text
        return response

    def purge_handler(self, vector: Any = None) -> WorkHandler:
        from knowledge_base.adapters.controller.tasks.purge_document_handler import (
            build_purge_document_handler,
        )

        return cast(
            WorkHandler,
            build_purge_document_handler(
                self.session_factory,
                self.indexing.extraction.settings,
                vector_index=vector or self.indexing.vector,
            ),
        )

    async def run_purge(
        self, document_id: UUID, handler: WorkHandler | None = None
    ) -> WorkOutcome:
        item_id = await self.item_id(f"purge_document:{document_id}")
        return await self.indexing.extraction.run(
            item_id, handler or self.purge_handler()
        )

    async def clean_orphans(self, grace: timedelta = GRACE) -> None:
        from knowledge_base.adapters.controller.composition import (
            build_clean_blob_orphans_handler,
        )
        from knowledge_base.use_cases.clean_blob_orphans.clean_blob_orphans_command import (  # noqa: E501
            CleanBlobOrphans,
        )

        handler = build_clean_blob_orphans_handler(
            self.session_factory, self.indexing.extraction.settings
        )
        await handler.handle(CleanBlobOrphans(grace=grace))

    # --- lectures ---

    async def item_id(self, dedup_key: str) -> UUID:
        rows = await self.indexing.extraction.imports.rows(
            "SELECT id FROM knowledge_base.work_items WHERE dedup_key = :k",
            k=dedup_key,
        )
        assert len(rows) == 1, f"item {dedup_key} : {len(rows)} ligne(s)"
        return cast(UUID, rows[0]["id"])

    async def items(self, document_id: UUID) -> list[dict[str, Any]]:
        return await self.indexing.extraction.imports.rows(
            "SELECT kind, status, dedup_key FROM knowledge_base.work_items "
            "WHERE payload->>'document_id' = :d ORDER BY dedup_key",
            d=str(document_id),
        )

    async def points_of(self, document_id: UUID) -> list[Record]:
        return [
            r
            for r in await self.indexing.points(self.index_id)
            if payload(r)["document_id"] == str(document_id)
        ]

    async def row(self, sql: str, **params: object) -> dict[str, Any]:
        (found,) = await self.indexing.extraction.imports.rows(sql, **params)
        return found

    async def count_chunks(self, document_id: UUID) -> int:
        found = await self.row(
            "SELECT count(*) AS n FROM knowledge_base.document_chunks c "
            "JOIN knowledge_base.document_versions v ON v.id = c.version_id "
            "WHERE v.document_id = :d",
            d=document_id,
        )
        return int(found["n"])

    async def count_units(self, document_id: UUID) -> int:
        found = await self.row(
            "SELECT count(*) AS n FROM knowledge_base.knowledge_units u "
            "JOIN knowledge_base.document_versions v ON v.id = u.version_id "
            "WHERE v.document_id = :d",
            d=document_id,
        )
        return int(found["n"])

    async def imported_version(self, document_id: UUID, version_id: UUID) -> Imported:
        """Version déjà importée, avec son item ``extract_version``."""
        (item,) = [
            i
            for i in await self.indexing.extraction.imports.extract_items()
            if i["payload"]["version_id"] == str(version_id)
        ]
        return Imported(document_id, version_id, item["id"])

    async def _imported(
        self, response: Response, document_id: UUID | None = None
    ) -> Imported:
        body = response.json()
        (item,) = [
            i
            for i in await self.indexing.extraction.imports.extract_items()
            if i["payload"]["version_id"] == body["version_id"]
        ]
        return Imported(
            document_id or UUID(body["document_id"]),
            UUID(body["version_id"]),
            item["id"],
        )


@asynccontextmanager
async def open_s7_rig(
    settings: Settings,
    session_factory: SessionFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[S7Rig]:
    embedder = HashEmbedder()
    async with open_indexing_rig(
        settings, session_factory, tmp_path, monkeypatch, embedder
    ) as indexing:
        index_id = await indexing.provision()
        search = compose_knowledge_search(
            session_factory,
            indexing.qdrant,
            kb_settings(),
            embedder=embedder,
            token_counter=COUNTER,
        )
        yield S7Rig(indexing, index_id, search)
