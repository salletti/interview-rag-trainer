"""Aides des tests d'intégration S9 (migration blue/green) : PostgreSQL, Qdrant
et work queue RÉELS ; embeddings par ``ProfileModel`` (``HashEmbedder`` à compteur
de tokens, aucun poids de modèle) sous DEUX profils : ``PROFILE`` (16
dimensions, index initial) et ``PROFILE_B`` (24 dimensions, index migré) ;
volume de blobs temporaire ; construites sur le harnais de S4 (``IndexingRig``).

Concurrence : jamais de ``sleep``. Les entrelacements sont imposés par des
barrières explicites (``asyncio.Event``) posées sur les frontières, et par le
verrou de ligne du corpus lui-même (une transaction qui tient ``FOR SHARE`` ou
``FOR UPDATE`` jusqu'à ce que le test la libère).

Composition NOUVELLE supposée (chemins sous ``knowledge_base`` ; les contrats
des use cases sont ceux de ``tests/unit/knowledge_base/use_cases/s9_support``) :

- ``EmbedderRegistry`` (``adapters/gateway/embedding/embedder_registry``) : le
  fournisseur d'embedders par profil ;
- ``build_index_version_handler(session_factory, settings, *, embedders,
  vector_index, ...)`` : ``embedders=`` à la place de ``embedder=`` ;
- ``adapters/controller/tasks/rechunk_version_handler.
  build_rechunk_version_handler(session_factory, settings, *, embedders,
  parser=None)`` (``kind`` ``rechunk_version``, file d'ingestion) ;
- ``adapters/controller/tasks/drop_index_collection_handler.
  build_drop_index_collection_handler(session_factory, *, collections)``
  (``kind`` ``drop_index_collection``, file d'ingestion) ;
- ``composition.build_start_index_migration_handler(session_factory,
  vector_index, *, id_factory=None, clock=None)`` et
  ``composition.build_activate_search_index_handler(session_factory, *,
  clock=None)`` ;
- ``composition.compose_knowledge_search(session_factory, qdrant_client,
  settings, *, embedders)`` ;
- ``QdrantVectorIndex.delete_collection(index_id)`` (idempotent), vu comme
  ``IndexCollections`` (port étroit, voir le support unitaire).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from httpx import AsyncClient
from qdrant_client.models import Record
from sqlalchemy import text

from knowledge_base import KnowledgeBaseWorkerResources
from knowledge_base.adapters.controller.composition import compose_knowledge_search
from knowledge_base.adapters.controller.tasks.execution import (
    WorkOutcome,
    execute_work_item,
)
from knowledge_base.adapters.controller.tasks.purge_document_handler import (
    build_purge_document_handler,
)
from knowledge_base.adapters.controller.tasks.reconcile_handler import (
    build_reconcile_work_handler,
)
from knowledge_base.adapters.controller.tasks.registry import WorkHandler
from knowledge_base.adapters.gateway.vector_store.qdrant_vector_index import (
    QdrantVectorIndex,
)
from knowledge_base.contracts.providers import KnowledgeSearch
from knowledge_base.contracts.result import KnowledgeQuery, KnowledgeSearchResult
from knowledge_base.domain.value_objects.chunking_policy import ChunkingPolicy
from knowledge_base.domain.value_objects.embedding_profile import EmbeddingProfile
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.extraction_support import (
    POLICY,
    Imported,
    test_parser,
)
from tests.integration.knowledge_base.indexing_support import (
    IndexingRig,
    open_indexing_rig,
    payload,
)
from tests.unit.knowledge_base.use_cases.indexing_fakes import PROFILE, HashEmbedder
from tests.unit.knowledge_base.use_cases.s9_support import (
    PROFILE_B,
    ProfileModel,
    activate_command,
    contract,
    start_command,
)

PREFIX = "/api/v1/knowledge-base"
TASKS = "knowledge_base.adapters.controller.tasks"
COMPOSITION = "knowledge_base.adapters.controller.composition"
EMBEDDER_REGISTRY = "knowledge_base.adapters.gateway.embedding.embedder_registry"
QUERY = "Qu'est-ce que Depends ?"

# Politique de l'index migré : même identité que celle de l'extraction (les lots
# existants servent tels quels) ou un autre budget (rechunking).
POLICY_SAME: ChunkingPolicy = POLICY
POLICY_SHORT = ChunkingPolicy(
    version="itest-2", tokenizer="test-words-v1", max_tokens=60
)


def DEFAULT_MODELS() -> tuple[ProfileModel, ...]:  # noqa: N802
    return (ProfileModel(PROFILE), ProfileModel(PROFILE_B))


BACKFILL_KINDS = frozenset({"rechunk_version", "index_version", "sync_visibility"})


def long_sheet(tag: str) -> bytes:
    """Une question, six paragraphes de 12 mots : un chunk sous ``POLICY_SAME``
    (120 mots), plusieurs sous ``POLICY_SHORT`` (60 mots)."""
    body = "\n\n".join(" ".join(f"p{tag}{n}x{i}" for i in range(12)) for n in range(6))
    return (
        f"# Fiche FastAPI\n\n## Question : Qu'est-ce que Depends {tag} ?\n\n{body}\n"
    ).encode()


def documents_of(result: KnowledgeSearchResult) -> set[UUID]:
    return {p.citation.document_id for p in result.passages}


@dataclass(frozen=True)
class S9Rig:
    indexing: IndexingRig
    index_id: UUID  # index actif initial (profil ``PROFILE``)
    models: tuple[ProfileModel, ...]

    # --- accès ---

    @property
    def provider(self) -> Any:
        """``EmbedderRegistry`` sur les modèles du rig (contrat S9)."""
        registry = contract(EMBEDDER_REGISTRY, "EmbedderRegistry")
        return registry({m.profile: (lambda m=m: m) for m in self.models})

    @property
    def http(self) -> AsyncClient:
        return self.indexing.extraction.imports.client

    @property
    def session_factory(self) -> SessionFactory:
        return self.indexing.extraction.session_factory

    @property
    def vector(self) -> QdrantVectorIndex:
        return self.indexing.vector

    @property
    def kb(self) -> Any:
        return self.indexing.extraction.settings

    # --- composition de S9 ---

    def index_handler(self, embedders: Any = None) -> WorkHandler:
        build = contract(
            f"{TASKS}.index_version_handler", "build_index_version_handler"
        )
        try:
            return cast(
                WorkHandler,
                build(
                    self.session_factory,
                    self.kb,
                    embedders=embedders or self.provider,
                    vector_index=self.vector,
                ),
            )
        except TypeError as error:
            pytest.fail(
                f"contrat S9 absent : build_index_version_handler(embedders=) "
                f"({error})",
                pytrace=False,
            )

    def rechunk_handler(self) -> WorkHandler:
        build = contract(
            f"{TASKS}.rechunk_version_handler", "build_rechunk_version_handler"
        )
        return cast(
            WorkHandler,
            build(
                self.session_factory,
                self.kb,
                embedders=self.provider,
                parser=test_parser(),
            ),
        )

    def drop_handler(self) -> WorkHandler:
        build = contract(
            f"{TASKS}.drop_index_collection_handler",
            "build_drop_index_collection_handler",
        )
        return cast(
            WorkHandler,
            build(self.session_factory, collections=self.vector),
        )

    def start_handler(self, vector: Any = None) -> Any:
        build = contract(COMPOSITION, "build_start_index_migration_handler")
        return build(self.session_factory, vector or self.vector)

    def activate_handler(self, **options: Any) -> Any:
        build = contract(COMPOSITION, "build_activate_search_index_handler")
        return build(self.session_factory, **options)

    def search_service(self, embedders: Any = None) -> KnowledgeSearch:
        try:
            compose: Any = compose_knowledge_search
            return cast(
                KnowledgeSearch,
                compose(
                    self.session_factory,
                    self.indexing.qdrant,
                    self.kb,
                    embedders=embedders or self.provider,
                ),
            )
        except TypeError as error:
            pytest.fail(
                f"contrat S9 absent : compose_knowledge_search(embedders=) ({error})",
                pytrace=False,
            )

    def handlers(self) -> tuple[WorkHandler, ...]:
        return (
            self.indexing.extraction.handler,
            self.index_handler(),
            self.indexing.sync_handler,
            self.rechunk_handler(),
            self.drop_handler(),
            cast(
                WorkHandler,
                build_purge_document_handler(
                    self.session_factory, self.kb, vector_index=self.vector
                ),
            ),
            cast(
                WorkHandler,
                build_reconcile_work_handler(
                    self.session_factory, self.kb, vector_index=self.vector
                ),
            ),
        )

    def resources(self, *override: WorkHandler) -> KnowledgeBaseWorkerResources:
        """Les handlers du rig ; ``override`` remplace celui du même ``kind``."""
        replaced = {h.kind: h for h in override}
        return KnowledgeBaseWorkerResources(
            session_factory=self.session_factory,
            loop=asyncio.get_running_loop(),
            handlers=tuple(replaced.get(h.kind, h) for h in self.handlers()),
        )

    # --- scénario ---

    async def import_doc(self, content: bytes, *, title: str = "Fiche") -> Imported:
        imports = self.indexing.extraction.imports
        response = await imports.post_document(
            content, kind="QA_SHEET", title=title, filename="fiche.md"
        )
        assert response.status_code == 202, response.text
        body = response.json()
        (item,) = [
            i
            for i in await imports.extract_items()
            if i["payload"]["version_id"] == body["version_id"]
        ]
        return Imported(UUID(body["document_id"]), UUID(body["version_id"]), item["id"])

    async def extract(self, imported: Imported) -> None:
        assert await self.run_id(imported.item_id) == "succeeded"

    async def import_extract(self, content: bytes, *, title: str = "Fiche") -> Imported:
        imported = await self.import_doc(content, title=title)
        await self.extract(imported)
        return imported

    async def publish(self, content: bytes, *, title: str = "Fiche") -> Imported:
        """Importé, extrait, indexé dans l'index ACTIF, publié, synchronisé."""
        imported = await self.import_extract(content, title=title)
        assert await self.run_index(imported.version_id, await self.active_id()) == (
            "succeeded"
        )
        await self.drain(kinds={"sync_visibility"})
        return imported

    async def start(
        self,
        profile: EmbeddingProfile = PROFILE_B,
        chunking_policy: ChunkingPolicy = POLICY_SAME,
    ) -> UUID:
        return cast(
            UUID,
            await self.start_handler().handle(start_command(profile, chunking_policy)),
        )

    async def activate(self, index_id: UUID, **options: Any) -> None:
        await self.activate_handler(**options).handle(activate_command(index_id))

    async def search(self, text: str = QUERY, limit: int = 20) -> KnowledgeSearchResult:
        return await self.search_service().search(
            KnowledgeQuery(text=text, limit=limit)
        )

    async def withdraw(self, document_id: UUID) -> None:
        response = await self.http.delete(f"{PREFIX}/documents/{document_id}")
        assert response.status_code == 202, response.text

    # --- exécution des items ---

    async def run_id(self, item_id: UUID, *override: WorkHandler) -> WorkOutcome:
        return await execute_work_item(
            item_id, self.resources(*override), self.kb, owner="s9-worker"
        )

    async def item_id(self, dedup_key: str) -> UUID:
        rows = await self.rows(
            "SELECT id FROM knowledge_base.work_items WHERE dedup_key = :k",
            k=dedup_key,
        )
        assert len(rows) == 1, f"item {dedup_key} : {len(rows)} ligne(s)"
        return cast(UUID, rows[0]["id"])

    async def run_key(self, dedup_key: str) -> WorkOutcome:
        return await self.run_id(await self.item_id(dedup_key))

    async def run_index(
        self, version_id: UUID, index_id: UUID, *override: WorkHandler
    ) -> WorkOutcome:
        item_id = await self.item_id(f"index_version:{version_id}:{index_id}")
        return await self.run_id(item_id, *override)

    async def drain(
        self,
        *,
        index_id: UUID | None = None,
        kinds: set[str] | frozenset[str] = BACKFILL_KINDS,
        rounds: int = 60,
    ) -> list[tuple[str, WorkOutcome]]:
        """Exécute les items ``PENDING`` de ``kinds`` jusqu'à épuisement ; avec
        ``index_id``, seulement ceux de cet index (les synchronisations de
        visibilité, sans index, passent toujours). Rend ``(kind, issue)``."""
        resources = self.resources()
        done: list[tuple[str, WorkOutcome]] = []
        for _ in range(rounds):
            rows = [
                r
                for r in await self.rows(
                    "SELECT id, kind, payload FROM knowledge_base.work_items "
                    "WHERE status = 'PENDING' AND available_at <= now() "
                    "ORDER BY created_at, id"
                )
                if r["kind"] in kinds
                and (
                    index_id is None
                    or r["kind"] == "sync_visibility"
                    or r["payload"].get("index_id") == str(index_id)
                )
            ]
            if not rows:
                return done
            for row in rows:
                done.append(
                    (
                        row["kind"],
                        await execute_work_item(
                            row["id"], resources, self.kb, owner="s9-drain"
                        ),
                    )
                )
        raise AssertionError(f"la file ne s'épuise pas : {done[-5:]}")

    # --- lectures ---

    async def rows(self, sql: str, **params: object) -> list[dict[str, Any]]:
        return await self.indexing.extraction.imports.rows(sql, **params)

    async def active_id(self) -> UUID:
        (row,) = await self.rows(
            "SELECT active_index_id FROM knowledge_base.knowledge_corpus"
        )
        return cast(UUID, row["active_index_id"])

    async def statuses(self) -> dict[UUID, str]:
        rows = await self.rows(
            "SELECT index_id, status FROM knowledge_base.search_indexes"
        )
        return {r["index_id"]: r["status"] for r in rows}

    async def indexations(self, index_id: UUID) -> dict[UUID, dict[str, Any]]:
        rows = await self.rows(
            "SELECT * FROM knowledge_base.indexations WHERE index_id = :i",
            i=index_id,
        )
        return {r["version_id"]: r for r in rows}

    async def items(self, kind: str) -> dict[str, dict[str, Any]]:
        rows = await self.rows(
            "SELECT * FROM knowledge_base.work_items WHERE kind = :k", k=kind
        )
        return {r["dedup_key"]: r for r in rows}

    async def chunk_count(self, version_id: UUID, policy: ChunkingPolicy) -> int:
        (row,) = await self.rows(
            "SELECT count(*) AS n FROM knowledge_base.document_chunks "
            "WHERE version_id = :v AND chunking_policy_version = :p",
            v=version_id,
            p=policy.identity,
        )
        return int(row["n"])

    async def points(self, index_id: UUID) -> list[Record]:
        return await self.indexing.points(index_id)

    async def point_document_ids(self, index_id: UUID) -> set[UUID]:
        return {UUID(payload(r)["document_id"]) for r in await self.points(index_id)}

    async def collection_exists(self, index_id: UUID) -> bool:
        return bool(
            await self.indexing.qdrant.collection_exists(
                QdrantVectorIndex.collection_name(index_id)
            )
        )

    async def vector_size(self, index_id: UUID) -> int:
        info = await self.indexing.qdrant.get_collection(
            QdrantVectorIndex.collection_name(index_id)
        )
        vectors = info.config.params.vectors
        assert vectors is not None and not isinstance(vectors, dict)
        return int(vectors.size)


@asynccontextmanager
async def open_s9_rig(
    settings: Settings,
    session_factory: SessionFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    models: Callable[[], tuple[ProfileModel, ...]] | None = None,
) -> AsyncIterator[S9Rig]:
    """Rig prêt : index actif initial (``PROFILE``, ``POLICY``), aucun document."""
    async with open_indexing_rig(
        settings, session_factory, tmp_path, monkeypatch, HashEmbedder()
    ) as indexing:
        index_id = await indexing.provision()
        built = models() if models is not None else DEFAULT_MODELS()
        yield S9Rig(indexing, index_id, built)


# --- barrières -----------------------------------------------------------------------

TIMEOUT = 20.0
LOCK_NOT_AVAILABLE = "55P03"


class _GatedCorpus:
    def __init__(
        self,
        inner: Any,
        on_lock: asyncio.Event | None,
        at_save: asyncio.Event | None,
        release_save: asyncio.Event | None,
    ) -> None:
        self._inner = inner
        self._on_lock = on_lock
        self._at_save = at_save
        self._release_save = release_save

    async def get_shared(self) -> Any:
        if self._on_lock is not None:
            self._on_lock.set()
        return await self._inner.get_shared()

    async def get_for_update(self) -> Any:
        if self._on_lock is not None:
            self._on_lock.set()
        return await self._inner.get_for_update()

    async def add(self, corpus: Any) -> None:
        await self._inner.add(corpus)

    async def save(self, corpus: Any) -> None:
        if self._at_save is not None:
            self._at_save.set()
        if self._release_save is not None:
            async with asyncio.timeout(TIMEOUT):
                await self._release_save.wait()
        await self._inner.save(corpus)


class GatedUnitOfWork:
    """Enveloppe une Unit of Work réelle : ``on_lock`` est signalé AVANT chaque
    lecture verrouillante du corpus (la requête qui peut attendre le verrou) ;
    ``at_save`` est signalé juste avant ``corpus.save`` (après la lecture du
    corpus et, pour l'activation, la preuve de couverture), puis la transaction
    attend ``release_save`` ; ``at_commit`` est signalé APRÈS toutes les
    écritures, juste avant le ``commit``, puis la transaction attend ``release``
    (verrous tenus)."""

    def __init__(
        self,
        inner: Any,
        *,
        on_lock: asyncio.Event | None = None,
        at_save: asyncio.Event | None = None,
        release_save: asyncio.Event | None = None,
        at_commit: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self._inner = inner
        self._at_commit = at_commit
        self._release = release
        self.corpus = _GatedCorpus(inner.corpus, on_lock, at_save, release_save)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def __aenter__(self) -> GatedUnitOfWork:
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._inner.__aexit__(*exc)

    async def commit(self) -> None:
        if self._at_commit is not None:
            self._at_commit.set()
        if self._release is not None:
            async with asyncio.timeout(TIMEOUT):
                await self._release.wait()
        await self._inner.commit()


class _GateAfterApply:
    """``WorkContext`` dont la finalisation attend, APRÈS le callback métier et
    avant le commit : la transaction (verrous compris) reste ouverte."""

    def __init__(
        self, inner: Any, reached: asyncio.Event, release: asyncio.Event
    ) -> None:
        self._inner = inner
        self._reached = reached
        self._release = release

    @property
    def finalized(self) -> bool:
        return bool(self._inner.finalized)

    @property
    def heartbeat_interval(self) -> Any:
        return self._inner.heartbeat_interval

    async def heartbeat(self) -> None:
        await self._inner.heartbeat()

    async def finalize(self, callback: Callable[[Any], Any]) -> None:
        async def held(session: Any) -> None:
            await callback(session)
            self._reached.set()
            async with asyncio.timeout(TIMEOUT):
                await self._release.wait()

        await self._inner.finalize(held)


class HoldFinalization:
    """Handler qui délègue au vrai et fige sa transaction de finalisation."""

    def __init__(
        self, inner: Any, reached: asyncio.Event, release: asyncio.Event
    ) -> None:
        self._inner = inner
        self._reached = reached
        self._release = release
        self.kind = inner.kind
        self.queue = inner.queue
        self.max_attempts = inner.max_attempts

    async def execute(self, item: Any, ctx: Any) -> None:
        await self._inner.execute(
            item, _GateAfterApply(ctx, self._reached, self._release)
        )

    async def on_terminal_failure(self, session: Any, item: Any, error: Any) -> None:
        await self._inner.on_terminal_failure(session, item, error)


def as_handler(held: HoldFinalization) -> WorkHandler:
    return cast(WorkHandler, held)


async def wait_until_a_backend_waits_for(
    session_factory: SessionFactory, statement_part: str
) -> None:
    """Rend la main quand PostgreSQL signale une requête en ATTENTE d'un verrou
    dont le texte contient ``statement_part`` (``pg_stat_activity``) : la
    barrière qui prouve qu'une transaction est bloquée par le verrou du corpus,
    sans délai. Une implémentation qui ne prend pas ce verrou n'attend jamais :
    le test échoue alors sur son délai maximal (``TIMEOUT``), pas sur un succès
    de hasard."""
    probe = text(
        "SELECT 1 FROM pg_stat_activity "
        "WHERE wait_event_type = 'Lock' AND datname = current_database() "
        "AND query LIKE :pattern AND pid <> pg_backend_pid()"
    )
    async with asyncio.timeout(TIMEOUT):
        while True:
            async with session_factory() as session:
                found = await session.execute(probe, {"pattern": f"%{statement_part}%"})
                if found.first() is not None:
                    return
