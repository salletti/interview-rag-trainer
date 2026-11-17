from __future__ import annotations

from collections.abc import AsyncIterable
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class ImportDocumentVersion:
    """Nouvelle version d'un document existant. ``content`` est consommé une
    seule fois."""

    document_id: UUID
    filename: str
    media_type: str
    content: AsyncIterable[bytes]
    idempotency_key: str | None = None
