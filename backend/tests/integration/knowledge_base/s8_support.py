"""Aides des tests d'intégration S8 (réconciliation, rejeu, échecs) :
PostgreSQL, Qdrant et work queue RÉELS, embeddings par ``HashEmbedder``, volume
de blobs temporaire ; construites sur le harnais de S7 (``S7Rig``).

Composition figée par S8 (en plus de celles de S2 à S7) :

- ``knowledge_base.adapters.controller.composition.
  build_reconcile_knowledge_base_handler(session_factory, vector_index,
  settings, *, clock=None, sampler=None) -> ReconcileKnowledgeBaseHandler`` :
  le cas d'usage sur le volume ``settings.blob_root``, les versions de
  PostgreSQL et ``vector_index``, qui doit aussi être un ``PointInventory``
  (``QdrantVectorIndex`` l'est). ``await handler.handle(ReconcileKnowledgeBase())
  -> ReconciliationReport``. ``sampler(candidates, k) -> sous-séquence`` choisit
  les indexations ``COMPLETED`` dont le manifest est revérifié (au plus
  ``settings.reconcile_manifest_sample_size``) ; défaut : tirage aléatoire.
- ``knowledge_base.adapters.controller.tasks.reconcile_handler.
  build_reconcile_work_handler(session_factory, settings, *, vector_index)`` et
  ``enqueue_reconciliation(session_factory, settings, *, now=None) -> UUID``.
- ``knowledge_base.adapters.controller.composition.
  build_retry_ingestion_handler(session_factory)`` et
  ``build_list_failures_handler(session_factory)`` ; routes REST montées par
  ``register`` (API réelle de ``create_app``).
- ``QdrantVectorIndex.scan_points(index_id, *, batch_size=256)`` (itérateur
  asynchrone de ``PointRecord``, ``domain.value_objects.point_record``).
- Migration : aucune colonne nouvelle n'est requise par ces tests.

Convergence : un cycle PLANIFIE ou APPLIQUE les corrections ; la « correction »
d'un test est observée une fois les items planifiés exécutés (``drain``) et le
cycle suivant propre (``settle``). Rien ne suppose la forme de la planification
(clé de dédoublonnage, kind) : seuls les effets sont vérifiés.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from httpx import AsyncClient
from qdrant_client.models import PointIdsList

from knowledge_base import KnowledgeBaseSettings, KnowledgeBaseWorkerResources
from knowledge_base.adapters.controller.composition import (
    build_reconcile_knowledge_base_handler,
)
from knowledge_base.adapters.controller.tasks.execution import execute_work_item
from knowledge_base.adapters.controller.tasks.reconcile_handler import (
    build_reconcile_work_handler,
)
from knowledge_base.adapters.controller.tasks.registry import WorkHandler
from knowledge_base.adapters.gateway.vector_store.qdrant_vector_index import (
    QdrantVectorIndex,
)
from knowledge_base.domain.services.point_id import point_id
from knowledge_base.domain.value_objects.indexed_point import IndexedPoint
from knowledge_base.use_cases.reconcile_knowledge_base.reconcile_knowledge_base_command import (  # noqa: E501
    ReconcileKnowledgeBase,
)
from knowledge_base.use_cases.reconcile_knowledge_base.reconcile_knowledge_base_handler import (  # noqa: E501
    ReconcileKnowledgeBaseHandler,
)
from knowledge_base.use_cases.reconcile_knowledge_base.reconciliation_report import (
    ReconciliationReport,
)
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.extraction_support import FAST_RETRY, Imported
from tests.integration.knowledge_base.indexing_support import IndexingRig, payload
from tests.integration.knowledge_base.s7_support import (
    PREFIX,
    S7Rig,
    open_s7_rig,
)
from tests.integration.knowledge_base.support import kb_settings
from tests.unit.knowledge_base.use_cases.indexing_fakes import hash_vector

Sampler = Callable[[Sequence[Any], int], Sequence[Any]]

OLD = timedelta(hours=25)
YOUNG = timedelta(minutes=5)
__all__ = ["OLD", "PREFIX", "YOUNG", "S8Rig", "Sampler", "open_s8_rig"]


def first_k(candidates: Sequence[Any], k: int) -> Sequence[Any]:
    """Échantillonneur déterministe : les ``k`` premiers candidats."""
    return list(candidates)[:k]


@dataclass(frozen=True)
class S8Rig:
    s7: S7Rig

    # --- accès ---

    @property
    def indexing(self) -> IndexingRig:
        return self.s7.indexing

    @property
    def session_factory(self) -> SessionFactory:
        return self.s7.session_factory

    @property
    def settings(self) -> KnowledgeBaseSettings:
        return self.s7.indexing.extraction.settings

    @property
    def vector(self) -> QdrantVectorIndex:
        return self.s7.indexing.vector

    @property
    def index_id(self) -> UUID:
        return self.s7.index_id

    @property
    def blob_root(self) -> Path:
        return self.s7.blob_root

    @property
    def http(self) -> AsyncClient:
        return self.s7.http

    # --- cycles ---

    def reconciler(
        self,
        *,
        settings: KnowledgeBaseSettings | None = None,
        sampler: Sampler | None = None,
        vector: Any = None,
    ) -> ReconcileKnowledgeBaseHandler:
        return build_reconcile_knowledge_base_handler(
            self.session_factory,
            vector or self.vector,
            settings or self.settings,
            sampler=sampler,
        )

    async def cycle(self, **kwargs: Any) -> ReconciliationReport:
        return await self.reconciler(**kwargs).handle(ReconcileKnowledgeBase())

    def handlers(self) -> tuple[WorkHandler, ...]:
        extraction = self.s7.indexing.extraction
        return (
            extraction.handler,
            self.s7.indexing.index_handler,
            self.s7.indexing.sync_handler,
            self.s7.purge_handler(),
            cast(
                WorkHandler,
                build_reconcile_work_handler(
                    self.session_factory, self.settings, vector_index=self.vector
                ),
            ),
        )

    def resources(self) -> KnowledgeBaseWorkerResources:
        return KnowledgeBaseWorkerResources(
            session_factory=self.session_factory,
            loop=asyncio.get_running_loop(),
            handlers=self.handlers(),
        )

    async def drain(self, *, rounds: int = 20) -> list[tuple[str, str]]:
        """Exécute les items ``PENDING`` disponibles jusqu'à épuisement (un item
        en crée d'autres : indexation, synchronisation) ; rend ``(kind, issue)``
        de chacun."""
        resources = self.resources()
        done: list[tuple[str, str]] = []
        for _ in range(rounds):
            rows = await self.rows(
                "SELECT id, kind FROM knowledge_base.work_items "
                "WHERE status = 'PENDING' AND available_at <= now() "
                "ORDER BY created_at, id"
            )
            if not rows:
                return done
            for row in rows:
                outcome = await execute_work_item(
                    row["id"], resources, self.settings, owner="drain"
                )
                done.append((row["kind"], outcome))
        raise AssertionError(f"la file ne s'épuise pas : {done[-5:]}")

    async def settle(self, *, cycles: int = 6) -> list[ReconciliationReport]:
        """Cycle, exécution du travail planifié, et ainsi de suite jusqu'à un
        cycle propre sans travail restant ; échoue si l'état ne converge pas."""
        reports: list[ReconciliationReport] = []
        for _ in range(cycles):
            report = await self.cycle()
            reports.append(report)
            if not await self.drain() and report.clean:
                return reports
        raise AssertionError(f"pas de convergence en {cycles} cycles : {reports}")

    # --- lectures SQL ---

    async def rows(self, sql: str, **params: object) -> list[dict[str, Any]]:
        return await self.s7.indexing.extraction.imports.rows(sql, **params)

    async def row(self, sql: str, **params: object) -> dict[str, Any]:
        return await self.s7.row(sql, **params)

    async def execute(self, sql: str, **params: object) -> None:
        from sqlalchemy import text

        async with self.session_factory() as session, session.begin():
            await session.execute(text(sql), params)

    async def items(
        self, kind: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        return await self.rows(
            "SELECT * FROM knowledge_base.work_items "
            "WHERE (CAST(:kind AS text) IS NULL OR kind = :kind) "
            "AND (CAST(:status AS text) IS NULL OR status = :status) "
            "ORDER BY created_at, id",
            kind=kind,
            status=status,
        )

    async def snapshot(self) -> list[tuple[str, str, str, int]]:
        """Tous les items (kind, clé, statut, tentatives) : ce qu'un cycle sans
        écart ne doit pas modifier."""
        return [
            (r["kind"], r["dedup_key"], r["status"], r["attempts"])
            for r in await self.items()
        ]

    async def version(self, version_id: UUID) -> dict[str, Any]:
        return await self.s7.indexing.extraction.version(version_id)

    async def indexation(self, version_id: UUID) -> dict[str, Any]:
        return await self.s7.indexing.indexation(version_id, self.index_id)

    async def document(self, document_id: UUID) -> dict[str, Any]:
        return await self.s7.indexing.document(document_id)

    async def item_of(self, kind: str, version_id: UUID) -> dict[str, Any]:
        found = [
            i
            for i in await self.items(kind)
            if i["payload"].get("version_id") == str(version_id)
        ]
        assert len(found) == 1, f"{kind} de {version_id} : {len(found)} item(s)"
        return found[0]

    async def visibility(self, document_id: UUID) -> list[bool]:
        return [
            bool(payload(r)["visible"]) for r in await self.s7.points_of(document_id)
        ]

    async def found(self, query_text: str | None = None) -> set[UUID]:
        from tests.integration.knowledge_base.s7_support import documents_of, query

        return documents_of(await self.s7.search.search(query()))

    # --- injections ---

    def fake_point(
        self, document_id: UUID, version_id: UUID, *, chunk_id: UUID | None = None
    ) -> IndexedPoint:
        chunk = chunk_id or uuid.uuid4()
        return IndexedPoint(
            point_id=point_id(self.index_id, chunk),
            vector=tuple(hash_vector("zombie", self.indexing.profile.dimension)),
            chunk_id=chunk,
            unit_id=uuid.uuid4(),
            version_id=version_id,
            document_id=document_id,
            chunk_hash="f" * 64,
            index_id=self.index_id,
            topics=("python",),
            kind="QA_SHEET",
            language="fr",
        )

    async def write_points(self, *points: IndexedPoint) -> None:
        await self.vector.upsert(self.index_id, list(points))

    async def hide(self, document_id: UUID, version_id: UUID) -> None:
        await self.vector.set_visibility(
            self.index_id,
            document_id=document_id,
            version_id=version_id,
            visible=False,
        )

    async def delete_point(self, point: UUID) -> None:
        await self.s7.indexing.qdrant.delete(
            collection_name=QdrantVectorIndex.collection_name(self.index_id),
            points_selector=PointIdsList(points=[str(point)]),
            wait=True,
        )

    async def corrupt_hash(self, point: UUID) -> None:
        await self.s7.indexing.qdrant.set_payload(
            collection_name=QdrantVectorIndex.collection_name(self.index_id),
            payload={"chunk_hash": "0" * 64},
            points=[str(point)],
            wait=True,
        )

    async def point_ids_of(self, document_id: UUID) -> set[UUID]:
        return {UUID(str(r.id)) for r in await self.s7.points_of(document_id)}

    def stage_upload(self, name: str, *, age: timedelta) -> Path:
        path = self.blob_root / "tmp" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"partiel")
        moment = (datetime.now(UTC) - age).timestamp()
        os.utime(path, (moment, moment))
        return path

    async def add_idempotency_key(self, key: str, *, age: timedelta) -> None:
        await self.execute(
            "INSERT INTO knowledge_base.idempotency_keys "
            "(key, fingerprint, document_id, version_id, number, created, created_at) "
            "VALUES (:k, 'f', :d, :v, 1, true, now() - make_interval(secs => :age))",
            k=key,
            d=uuid.uuid4(),
            v=uuid.uuid4(),
            age=age.total_seconds(),
        )

    async def idempotency_keys(self) -> set[str]:
        return {
            r["key"]
            for r in await self.rows("SELECT key FROM knowledge_base.idempotency_keys")
        }

    async def fail_item(
        self, item_id: UUID, code: str, *, attempts: int = 5, message: str = "x"
    ) -> None:
        """Pose l'état que laisserait ``recover`` ou un hook en échec : item
        ``FAILED`` sans que l'agrégat métier ne le sache."""
        await self.execute(
            "UPDATE knowledge_base.work_items SET status = 'FAILED', attempts = :a, "
            "last_error_code = :c, last_error_message = :m, completed_at = now(), "
            "lease_owner = NULL, lease_expires_at = NULL WHERE id = :id",
            a=attempts,
            c=code,
            m=message,
            id=item_id,
        )

    async def publish_in_two_documents(self) -> tuple[Imported, Imported]:
        from tests.integration.knowledge_base.indexing_support import QA_SHEET
        from tests.integration.knowledge_base.s7_support import PYDANTIC_SHEET

        first = await self.s7.publish(QA_SHEET, title="Premier")
        second = await self.s7.publish(PYDANTIC_SHEET, title="Second")
        return first, second


@asynccontextmanager
async def open_s8_rig(
    settings: Settings,
    session_factory: SessionFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[S8Rig]:
    async with open_s7_rig(settings, session_factory, tmp_path, monkeypatch) as s7:
        yield S8Rig(s7)


def settings_with(**overrides: float) -> KnowledgeBaseSettings:
    """Réglages de test (reprise rapide) avec surcharges, lus après le
    positionnement de ``KB_BLOB_ROOT`` par le rig."""
    return kb_settings(**{**FAST_RETRY, **overrides})
