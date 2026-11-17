"""Aides des tests d'intégration S2 : application réelle (``create_app``),
POST multipart via httpx ``ASGITransport``, lectures SQL et du volume de blobs.

Le volume est un répertoire temporaire (``KB_BLOB_ROOT``) et la limite d'upload
est réduite (``KB_MAX_UPLOAD_BYTES``) : ``KnowledgeBaseSettings`` est donc relu
de l'environnement à la construction de l'application (pas de cache global).
L'exception d'une requête en erreur devient une réponse 500
(``raise_app_exceptions=False``) pour que les tests d'atomicité observent l'état
laissé derrière.

Contrat HTTP figé (préfixe ``/api/v1/knowledge-base``) :

- ``POST /documents`` (multipart : ``file``, ``title``, ``kind``, ``language``,
  ``topics`` répété) ; ``POST /documents/{document_id}/versions`` (``file``) ;
  en-tête facultatif ``Idempotency-Key``.
- 202 ``{"document_id", "version_id", "number"}`` ; 200 avec le même corps si le
  contenu était déjà connu ; erreurs ``{"code", "message", ...}`` :
  413 ``PAYLOAD_TOO_LARGE``, 415 ``UNSUPPORTED_MEDIA_TYPE``, 404
  ``DOCUMENT_NOT_FOUND``, 409 ``DUPLICATE_CONTENT`` (avec ``document_id`` et
  ``version_id`` existants), 409 ``IDEMPOTENCY_KEY_CONFLICT``, 409
  ``DOCUMENT_WITHDRAWN``, 409 ``CONCURRENT_MODIFICATION``, 422 (formulaire
  invalide).
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text

from runtime.app import create_app
from runtime.database import SessionFactory
from runtime.settings import Settings

PREFIX = "/api/v1/knowledge-base"
MAX_UPLOAD_BYTES = 64 * 1024
MARKDOWN = "text/markdown"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def blob_path(root: Path, data: bytes) -> Path:
    digest = sha256(data)
    return root / "sha256" / digest[:2] / digest[2:4] / digest


@dataclass(frozen=True)
class ImportRig:
    client: AsyncClient
    blob_root: Path
    session_factory: SessionFactory

    async def post_document(
        self,
        content: bytes,
        *,
        filename: str = "cours.md",
        media_type: str = MARKDOWN,
        title: str = "Python avancé",
        kind: str = "COURSE",
        language: str = "fr",
        topics: Sequence[str] = ("python", "asyncio"),
        idempotency_key: str | None = None,
        omit: Sequence[str] = (),
    ) -> Response:
        data: dict[str, Any] = {
            "title": title,
            "kind": kind,
            "language": language,
            "topics": list(topics),
        }
        for field in omit:
            del data[field]
        return await self.client.post(
            f"{PREFIX}/documents",
            data=data,
            files={"file": (filename, content, media_type)},
            headers=_headers(idempotency_key),
        )

    async def post_version(
        self,
        document_id: UUID | str,
        content: bytes,
        *,
        filename: str = "cours.md",
        media_type: str = MARKDOWN,
        idempotency_key: str | None = None,
    ) -> Response:
        return await self.client.post(
            f"{PREFIX}/documents/{document_id}/versions",
            files={"file": (filename, content, media_type)},
            headers=_headers(idempotency_key),
        )

    def blob_files(self) -> list[Path]:
        if not self.blob_root.exists():
            return []
        return sorted(p for p in self.blob_root.rglob("*") if p.is_file())

    async def rows(self, sql: str, **params: object) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            result = await session.execute(text(sql), params)
            return [dict(row) for row in result.mappings()]

    async def documents(self) -> list[dict[str, Any]]:
        return await self.rows("SELECT * FROM knowledge_base.documents")

    async def versions(self, document_id: UUID | str) -> list[dict[str, Any]]:
        return await self.rows(
            "SELECT * FROM knowledge_base.document_versions "
            "WHERE document_id = :id ORDER BY number",
            id=UUID(str(document_id)),
        )

    async def count(self, table: str) -> int:
        (row,) = await self.rows(f"SELECT count(*) AS n FROM knowledge_base.{table}")
        return int(row["n"])

    async def extract_items(self) -> list[dict[str, Any]]:
        return await self.rows(
            "SELECT * FROM knowledge_base.work_items "
            "WHERE kind = 'extract_version' ORDER BY created_at"
        )


def _headers(idempotency_key: str | None) -> dict[str, str]:
    return {} if idempotency_key is None else {"Idempotency-Key": idempotency_key}


async def clean_documents(session_factory: SessionFactory) -> None:
    """Vide les tables de S2 (``work_items`` l'est déjà par la fixture autouse
    de ``conftest``)."""
    async with session_factory() as session, session.begin():
        await session.execute(text("DELETE FROM knowledge_base.idempotency_keys"))
        await session.execute(text("DELETE FROM knowledge_base.document_versions"))
        await session.execute(text("DELETE FROM knowledge_base.documents"))


@asynccontextmanager
async def open_import_rig(
    settings: Settings,
    session_factory: SessionFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[ImportRig]:
    blob_root = tmp_path / "blobs"
    monkeypatch.setenv("KB_BLOB_ROOT", str(blob_root))
    monkeypatch.setenv("KB_MAX_UPLOAD_BYTES", str(MAX_UPLOAD_BYTES))
    await clean_documents(session_factory)
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as http,
    ):
        yield ImportRig(http, blob_root, session_factory)
