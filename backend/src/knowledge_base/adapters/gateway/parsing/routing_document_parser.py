"""Aiguillage vers le parseur du type de média (ADR 0008).

La composition de production (``default_parser``) y met Docling pour
``text/markdown``, ``application/pdf`` et le DOCX, et ``PlainTextParser`` pour
``text/plain``.
"""

from __future__ import annotations

from collections.abc import Mapping

from knowledge_base.domain.exceptions.parsing_errors import (
    INVALID_DOCUMENT,
    DocumentParseError,
)
from knowledge_base.domain.providers.document_parser import DocumentParser
from knowledge_base.domain.value_objects.structured_document import StructuredDocument


class RoutingDocumentParser:
    def __init__(self, parsers: Mapping[str, DocumentParser]) -> None:
        self._parsers = {_essence(media): parser for media, parser in parsers.items()}

    def parse(
        self, content: bytes, media_type: str, filename: str
    ) -> StructuredDocument:
        parser = self._parsers.get(_essence(media_type))
        if parser is None:
            raise DocumentParseError(
                INVALID_DOCUMENT,
                f"type de média {media_type!r} non pris en charge pour {filename}",
            )
        return parser.parse(content, media_type, filename)


def _essence(media_type: str) -> str:
    """Type sans paramètre (``; charset=…``) ni casse."""
    return media_type.split(";", 1)[0].strip().lower()
