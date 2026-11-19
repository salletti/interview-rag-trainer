"""``RoutingDocumentParser`` : un parseur par type de média (ADR 0008).

API figée (``knowledge_base.adapters.gateway.parsing.routing_document_parser``) :
``RoutingDocumentParser(parsers: Mapping[str, DocumentParser])`` est lui-même un
``DocumentParser`` ; il délègue au parseur du ``media_type`` (comparé sans
paramètre ni casse), et lève ``DocumentParseError(INVALID_DOCUMENT)`` pour un
type non pris en charge. La composition de production y met Docling pour
``text/markdown`` et ``PlainTextParser`` pour ``text/plain`` (PDF/DOCX : S6).
"""

from __future__ import annotations

import pytest

from knowledge_base.adapters.gateway.parsing.plain_text_parser import PlainTextParser
from knowledge_base.adapters.gateway.parsing.routing_document_parser import (
    RoutingDocumentParser,
)
from knowledge_base.domain.exceptions.parsing_errors import (
    INVALID_DOCUMENT,
    DocumentParseError,
)
from knowledge_base.domain.value_objects.structured_document import (
    Heading,
    Paragraph,
    StructuredDocument,
)
from tests.unit.knowledge_base.markdown_lite import MarkdownLiteParser


def router() -> RoutingDocumentParser:
    return RoutingDocumentParser(
        {"text/markdown": MarkdownLiteParser(), "text/plain": PlainTextParser()}
    )


def test_markdown_goes_to_the_markdown_parser() -> None:
    document = router().parse(b"# Titre\n\ntexte\n", "text/markdown", "a.md")

    assert document.blocks == (Heading(level=1, text="Titre"), Paragraph(text="texte"))


def test_plain_text_goes_to_the_plain_text_parser() -> None:
    document = router().parse(b"# pas un titre\n", "text/plain", "a.txt")

    assert document == StructuredDocument(blocks=(Paragraph(text="# pas un titre"),))


@pytest.mark.parametrize("media_type", ["text/plain; charset=utf-8", "TEXT/PLAIN"])
def test_the_media_type_is_compared_without_parameters_or_case(media_type: str) -> None:
    document = router().parse(b"# pas un titre\n", media_type, "a.txt")

    assert document.blocks == (Paragraph(text="# pas un titre"),)


def test_an_unsupported_media_type_is_a_terminal_invalid_document() -> None:
    with pytest.raises(DocumentParseError) as raised:
        router().parse(b"%PDF-1.7", "application/pdf", "a.pdf")

    assert raised.value.code == INVALID_DOCUMENT
