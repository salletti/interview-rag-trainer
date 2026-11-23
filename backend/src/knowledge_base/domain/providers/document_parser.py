"""Port d'extraction d'un original en ``StructuredDocument`` (ADR 0008 §2-3)."""

from __future__ import annotations

from typing import Protocol

from knowledge_base.domain.value_objects.structured_document import StructuredDocument


class DocumentParser(Protocol):
    def parse(
        self, content: bytes, media_type: str, filename: str
    ) -> StructuredDocument:
        """Synchrone : CPU pur, appelé hors de la boucle asyncio. Lève
        ``DocumentParseError`` pour un contenu vide ou illisible."""
        ...
