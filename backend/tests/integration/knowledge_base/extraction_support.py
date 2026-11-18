"""Aides des tests d'intégration S3 : import réel par l'API de S2, puis
exécution de l'item ``extract_version`` par ``execute_work_item`` (S0), sur
PostgreSQL réel et un volume de blobs temporaire.

Le parseur Markdown est ``MarkdownLiteParser`` (double de la frontière Docling)
sauf dans les tests qui exigent le vrai Docling (dépendance ferme). Compteur
de tokens : un mot = un token ; politique de test ``itest-1`` à 120 tokens,
tokenizer ``test-words-v1`` (identité persistée ``itest-1+test-words-v1``).

Tables figées par ces tests (schéma ``knowledge_base``, cf.
``test_extraction_migration``) : ``knowledge_units``, ``document_chunks``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from knowledge_base import KnowledgeBaseSettings, KnowledgeBaseWorkerResources
from knowledge_base.adapters.controller.tasks.execution import (
    WorkOutcome,
    execute_work_item,
)
from knowledge_base.adapters.controller.tasks.extract_version_handler import (
    ExtractVersionWorkHandler,
    build_extract_version_handler,
)
from knowledge_base.adapters.controller.tasks.registry import WorkHandler
from knowledge_base.adapters.gateway.parsing.plain_text_parser import PlainTextParser
from knowledge_base.adapters.gateway.parsing.routing_document_parser import (
    RoutingDocumentParser,
)
from knowledge_base.domain.providers.document_parser import DocumentParser
from knowledge_base.domain.value_objects.chunking_policy import ChunkingPolicy
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.import_support import (
    ImportRig,
    open_import_rig,
)
from tests.integration.knowledge_base.support import fetch_item, kb_settings
from tests.unit.knowledge_base.domain.services.support import COUNTER
from tests.unit.knowledge_base.markdown_lite import MarkdownLiteParser

POLICY = ChunkingPolicy(version="itest-1", tokenizer="test-words-v1", max_tokens=120)
FAST_RETRY = {"work_backoff_base_seconds": 0.01}


def test_parser() -> DocumentParser:
    return RoutingDocumentParser(
        {"text/markdown": MarkdownLiteParser(), "text/plain": PlainTextParser()}
    )


@dataclass(frozen=True)
class Imported:
    document_id: UUID
    version_id: UUID
    item_id: UUID


@dataclass(frozen=True)
class ExtractionRig:
    imports: ImportRig
    settings: KnowledgeBaseSettings
    handler: ExtractVersionWorkHandler

    @property
    def session_factory(self) -> SessionFactory:
        return self.imports.session_factory

    def handler_with(self, parser: DocumentParser | None) -> ExtractVersionWorkHandler:
        return build_extract_version_handler(
            self.session_factory,
            self.settings,
            parser=parser,
            token_counter=COUNTER,
            policy=POLICY,
        )

    async def import_document(
        self,
        content: bytes,
        *,
        kind: str = "COURSE",
        filename: str = "cours.md",
        media_type: str = "text/markdown",
    ) -> Imported:
        response = await self.imports.post_document(
            content, kind=kind, filename=filename, media_type=media_type
        )
        assert response.status_code == 202, response.text
        body = response.json()
        (item,) = [
            i
            for i in await self.imports.extract_items()
            if i["payload"]["version_id"] == body["version_id"]
        ]
        return Imported(UUID(body["document_id"]), UUID(body["version_id"]), item["id"])

    async def run(
        self,
        item_id: UUID,
        handler: WorkHandler | None = None,
        settings: KnowledgeBaseSettings | None = None,
    ) -> WorkOutcome:
        resources = KnowledgeBaseWorkerResources(
            session_factory=self.session_factory,
            loop=asyncio.get_running_loop(),
            handlers=(handler or self.handler,),
        )
        return await execute_work_item(
            item_id, resources, settings or self.settings, owner="worker-test"
        )

    # --- lectures ---------------------------------------------------------------

    async def version(self, version_id: UUID) -> dict[str, Any]:
        (row,) = await self.imports.rows(
            "SELECT * FROM knowledge_base.document_versions WHERE id = :id",
            id=version_id,
        )
        return row

    async def item(self, item_id: UUID) -> dict[str, Any]:
        return await fetch_item(self.session_factory, item_id)

    async def units(self, version_id: UUID) -> list[dict[str, Any]]:
        return await self.imports.rows(
            "SELECT * FROM knowledge_base.knowledge_units "
            "WHERE version_id = :id ORDER BY ordinal",
            id=version_id,
        )

    async def chunks(self, version_id: UUID) -> list[dict[str, Any]]:
        return await self.imports.rows(
            "SELECT c.* FROM knowledge_base.document_chunks c "
            "JOIN knowledge_base.knowledge_units u ON u.id = c.unit_id "
            "WHERE c.version_id = :id ORDER BY u.ordinal, c.ordinal_in_unit",
            id=version_id,
        )

    async def counts(self) -> tuple[int, int]:
        return (
            await self.imports.count("knowledge_units"),
            await self.imports.count("document_chunks"),
        )


async def clean_extraction(session_factory: SessionFactory) -> None:
    async with session_factory() as session, session.begin():
        await session.execute(text("DELETE FROM knowledge_base.document_chunks"))
        await session.execute(text("DELETE FROM knowledge_base.knowledge_units"))


@asynccontextmanager
async def open_extraction_rig(
    settings: Settings,
    session_factory: SessionFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[ExtractionRig]:
    await clean_extraction(session_factory)
    async with open_import_rig(
        settings, session_factory, tmp_path, monkeypatch
    ) as imports:
        # Relus de l'environnement posé par le rig d'import (KB_BLOB_ROOT).
        kb = kb_settings(**FAST_RETRY)
        rig = ExtractionRig(
            imports=imports,
            settings=kb,
            handler=build_extract_version_handler(
                session_factory,
                kb,
                parser=test_parser(),
                token_counter=COUNTER,
                policy=POLICY,
            ),
        )
        yield rig
        await clean_extraction(session_factory)
