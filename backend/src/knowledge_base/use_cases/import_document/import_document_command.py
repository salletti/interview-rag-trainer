from __future__ import annotations

from collections.abc import AsyncIterable
from dataclasses import dataclass

from knowledge_base.domain.value_objects.document_kind import DocumentKind


@dataclass(frozen=True)
class ImportDocument:
    """Import d'un nouveau document. ``content`` est consommé une seule fois."""

    title: str
    kind: DocumentKind
    topics: tuple[str, ...]
    language: str
    filename: str
    media_type: str
    content: AsyncIterable[bytes]
    idempotency_key: str | None = None
