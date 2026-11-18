"""Vérifications de ``DoclingPdfParser`` AVANT le pipeline de layout (S6 ;
knowledge-base.md, précisions S6) : couche texte et nombre de pages, lus par
pypdfium2 sans aucun modèle.

La conversion Docling est remplacée par un double (``DoclingConversion.convert``,
commune aux parseurs Docling) : ces tests
vérifient ce qui est décidé avant elle, et qu'elle n'est appelée que si le PDF
passe les contrôles.
"""

from __future__ import annotations

import io
import logging

import pypdfium2
import pytest
from docling_core.types.doc import DocItemLabel, DoclingDocument

from knowledge_base.adapters.gateway.parsing.docling_conversion import (
    DoclingConversion,
)
from knowledge_base.adapters.gateway.parsing.docling_pdf_parser import (
    DoclingPdfParser,
)
from knowledge_base.domain.exceptions.parsing_errors import (
    DocumentParseError,
    ParseErrorCode,
)
from knowledge_base.domain.value_objects.structured_document import Paragraph
from tests.office_support import PDF, minimal_pdf, office_bytes


@pytest.fixture
def conversions(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Noms des fichiers réellement confiés à Docling (double sans modèle)."""
    converted: list[str] = []

    def convert(self: DoclingConversion, content: bytes, filename: str) -> object:
        converted.append(filename)
        document = DoclingDocument(name="double")
        document.add_text(label=DocItemLabel.TEXT, text="Texte converti.")
        return document

    monkeypatch.setattr(DoclingConversion, "convert", convert)
    return converted


def merged(*parts: tuple[bytes, list[int]]) -> bytes:
    """Un PDF fait des pages choisies de plusieurs PDF."""
    target = pypdfium2.PdfDocument.new()
    for content, pages in parts:
        target.import_pages(pypdfium2.PdfDocument(content), pages)
    buffer = io.BytesIO()
    target.save(buffer)
    return buffer.getvalue()


def test_a_pdf_without_any_text_layer_requires_ocr_without_conversion(
    conversions: list[str],
) -> None:
    with pytest.raises(DocumentParseError) as raised:
        DoclingPdfParser().parse(office_bytes("scanned.pdf"), PDF, "scan.pdf")

    assert raised.value.code == ParseErrorCode.OCR_REQUIRED
    assert "scan.pdf" in raised.value.message
    assert conversions == []


def test_a_partially_scanned_pdf_is_converted_and_its_blank_pages_logged(
    conversions: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    content = merged(
        (office_bytes("pages.pdf"), [0, 1]), (office_bytes("scanned.pdf"), [0])
    )

    with caplog.at_level(logging.WARNING):
        document = DoclingPdfParser().parse(content, PDF, "mixte.pdf")

    assert document.blocks == (Paragraph(text="Texte converti."),)
    assert conversions == ["mixte.pdf"]
    assert "mixte.pdf : 1 page(s) sans couche texte sur 3" in caplog.text


def test_a_pdf_of_exactly_max_pages_pages_is_converted(
    conversions: list[str],
) -> None:
    DoclingPdfParser(max_pages=2).parse(minimal_pdf(2), PDF, "deux.pdf")
    DoclingPdfParser().parse(minimal_pdf(300), PDF, "trois-cents.pdf")

    assert conversions == ["deux.pdf", "trois-cents.pdf"]


def test_a_refused_pdf_never_reaches_the_conversion(conversions: list[str]) -> None:
    for content in (minimal_pdf(301), office_bytes("encrypted.pdf"), b""):
        with pytest.raises(DocumentParseError):
            DoclingPdfParser().parse(content, PDF, "refuse.pdf")

    assert conversions == []


def test_a_pdf_whose_conversion_has_no_block_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        DoclingConversion,
        "convert",
        lambda self, content, filename: DoclingDocument(name="vide"),
    )

    with pytest.raises(DocumentParseError) as raised:
        DoclingPdfParser().parse(minimal_pdf(1), PDF, "vide.pdf")

    assert raised.value.code == ParseErrorCode.EMPTY_DOCUMENT
