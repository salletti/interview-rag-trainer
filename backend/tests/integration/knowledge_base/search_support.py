"""Aides des tests d'intégration S5 : recherche de bout en bout sur PostgreSQL
et Qdrant réels, avec le VRAI modèle ``multilingual-e5-base`` (marqueur
``model``).

Composition figée (en plus de celles de S3 et S4, ``indexing_support``) :

- ``knowledge_base.build_knowledge_search(session_factory, qdrant_client,
  settings=None, *, embedder=None) -> KnowledgeSearch`` (voir
  ``tests/unit/knowledge_base/test_build_knowledge_search.py``) ;
- l'API (``runtime.app.create_app``) sert ``POST /api/v1/knowledge-base/search``
  avec SON propre embedder paresseux, dont le cache est ``KB_MODELS_CACHE_DIR``
  (positionné ici sur le cache du Makefile, ``SENTENCE_TRANSFORMERS_HOME``) ;
- les documents sont publiés comme en production : import par l'API (S2),
  item ``extract_version`` (S3), item ``index_version`` (publication, S4) puis
  ``sync_visibility`` (qui rend les points visibles).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from knowledge_base import build_knowledge_search
from knowledge_base.contracts.providers import KnowledgeSearch
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.extraction_support import Imported
from tests.integration.knowledge_base.indexing_support import (
    IndexingRig,
    make_embedder,
    open_indexing_rig,
)
from tests.integration.knowledge_base.support import kb_settings

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "knowledge_base"
BACKEND_CACHE = Path(__file__).resolve().parents[3] / ".cache" / "models"


def models_cache() -> str:
    return os.environ.get("SENTENCE_TRANSFORMERS_HOME", str(BACKEND_CACHE))


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@dataclass(frozen=True)
class SearchRig:
    indexing: IndexingRig
    index_id: UUID
    search: KnowledgeSearch

    @property
    def http(self) -> AsyncClient:
        return self.indexing.extraction.imports.client

    @property
    def session_factory(self) -> SessionFactory:
        return self.indexing.extraction.session_factory

    async def publish(
        self,
        content: bytes,
        *,
        title: str = "Fiche de révision",
        kind: str = "QA_SHEET",
        language: str = "fr",
        topics: tuple[str, ...] = ("python",),
        filename: str = "fiche.md",
    ) -> Imported:
        """Import, extraction, indexation, publication, synchronisation : le
        document est visible à la recherche."""
        imported = await self.import_only(
            content,
            title=title,
            kind=kind,
            language=language,
            topics=topics,
            filename=filename,
        )
        await self.index_and_publish(imported)
        await self.synchronize(imported.document_id)
        return imported

    async def import_only(
        self,
        content: bytes,
        *,
        title: str = "Fiche de révision",
        kind: str = "QA_SHEET",
        language: str = "fr",
        topics: tuple[str, ...] = ("python",),
        filename: str = "fiche.md",
    ) -> Imported:
        """Import puis extraction (version ``EXTRACTED``, chunks en base)."""
        imports = self.indexing.extraction.imports
        response = await imports.post_document(
            content,
            filename=filename,
            title=title,
            kind=kind,
            language=language,
            topics=topics,
        )
        assert response.status_code == 202, response.text
        body = response.json()
        (item,) = [
            i
            for i in await imports.extract_items()
            if i["payload"]["version_id"] == body["version_id"]
        ]
        imported = Imported(
            UUID(body["document_id"]), UUID(body["version_id"]), item["id"]
        )
        assert await self.indexing.extraction.run(imported.item_id) == "succeeded"
        return imported

    async def index_and_publish(self, imported: Imported) -> None:
        assert await self.indexing.run_index(imported.version_id, self.index_id) == (
            "succeeded"
        )

    async def synchronize(self, document_id: UUID) -> None:
        revision = (await self.indexing.document(document_id))["visibility_revision"]
        assert await self.indexing.run_sync(document_id, revision) == "succeeded"

    async def execute(self, sql: str, **params: Any) -> None:
        async with self.session_factory() as session, session.begin():
            await session.execute(text(sql), params)


@asynccontextmanager
async def open_search_rig(
    settings: Settings,
    session_factory: SessionFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    **kb_overrides: float,
) -> AsyncIterator[SearchRig]:
    monkeypatch.setenv("KB_MODELS_CACHE_DIR", models_cache())
    embedder = make_embedder("model")
    async with open_indexing_rig(
        settings, session_factory, tmp_path, monkeypatch, embedder
    ) as indexing:
        index_id = await indexing.provision()
        search = build_knowledge_search(
            session_factory,
            indexing.qdrant,
            kb_settings(**kb_overrides),
            embedder=embedder,
        )
        yield SearchRig(indexing, index_id, search)
