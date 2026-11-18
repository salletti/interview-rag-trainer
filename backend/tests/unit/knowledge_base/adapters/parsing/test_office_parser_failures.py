"""Échecs déterministes des parseurs PDF et DOCX de S6, sans modèle de layout
(roadmap S6 ; ADR 0008, point 5 ; ADR 0012, point 4).

API supposée : ``DoclingPdfParser(*, max_pages: int = MAX_PDF_PAGES)`` dans
``adapters/gateway/parsing/docling_pdf_parser.py`` (``MAX_PDF_PAGES = 300``) et
``DoclingDocxParser()`` dans ``docling_docx_parser.py`` ; l'un et l'autre sont
des ``DocumentParser`` (``parse(content, media_type, filename)``). Aucun type
Docling ne sort : tout échec est une ``DocumentParseError``.

Ces échecs sont détectés AVANT le pipeline de layout (qui charge des modèles
et coûte des secondes par page) : ces tests tournent sans poids de modèle et
doivent rester rapides. Ils exigent les extras Docling ``format-pdf`` (comptage
des pages, détection du chiffrement) et ``format-docx``, absents du lock.

- PDF chiffré ou corrompu, DOCX illisible : ``INVALID_DOCUMENT``.
- Plus de ``max_pages`` pages : ``TOO_MANY_PAGES`` (le message donne le nombre
  de pages du document et la limite). La limite ne s'applique qu'au PDF : un
  DOCX n'a pas de pagination avant sa mise en page.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from knowledge_base.adapters.gateway.parsing.docling_docx_parser import (
    DoclingDocxParser,
)
from knowledge_base.adapters.gateway.parsing.docling_pdf_parser import (
    MAX_PDF_PAGES,
    DoclingPdfParser,
)
from knowledge_base.domain.exceptions.parsing_errors import (
    INVALID_DOCUMENT,
    DocumentParseError,
    ParseErrorCode,
)
from tests.office_support import DOCX, PDF, minimal_pdf, office_bytes


def test_the_default_page_limit_is_the_300_pages_of_adr_0012() -> None:
    assert MAX_PDF_PAGES == 300


def test_a_pdf_over_the_default_limit_is_refused_with_its_page_count() -> None:
    with pytest.raises(DocumentParseError) as raised:
        DoclingPdfParser().parse(minimal_pdf(301), PDF, "long.pdf")

    assert raised.value.code == ParseErrorCode.TOO_MANY_PAGES
    assert "301" in raised.value.message
    assert "300" in raised.value.message


def test_the_page_limit_is_configurable_and_exclusive_of_the_limit_itself() -> None:
    parser = DoclingPdfParser(max_pages=2)

    with pytest.raises(DocumentParseError) as raised:
        parser.parse(minimal_pdf(3), PDF, "trois-pages.pdf")

    assert raised.value.code == ParseErrorCode.TOO_MANY_PAGES


@pytest.mark.parametrize(
    "name", ["encrypted.pdf", "corrupted.pdf"], ids=["encrypted", "corrupted"]
)
def test_an_encrypted_or_corrupted_pdf_is_a_terminal_invalid_document(
    name: str,
) -> None:
    with pytest.raises(DocumentParseError) as raised:
        DoclingPdfParser().parse(office_bytes(name), PDF, name)

    assert raised.value.code == INVALID_DOCUMENT
    assert name in raised.value.message


@pytest.mark.parametrize(
    "content",
    [b"", b"ceci n'est pas un pdf", b"%PDF-1.7\n"],
    ids=["empty", "text", "header"],
)
def test_bytes_that_are_not_a_pdf_are_a_terminal_invalid_document(
    content: bytes,
) -> None:
    with pytest.raises(DocumentParseError) as raised:
        DoclingPdfParser().parse(content, PDF, "faux.pdf")

    assert raised.value.code == INVALID_DOCUMENT


def not_a_word_document() -> bytes:
    """Un ZIP valide qui n'est pas un document Word (pas de ``word/``)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("lisez-moi.txt", "pas du OOXML")
    return buffer.getvalue()


@pytest.mark.parametrize(
    "content",
    [b"", b"ceci n'est pas un docx", b"PK\x03\x04tronque", not_a_word_document()],
    ids=["empty", "text", "truncated-zip", "zip-without-word"],
)
def test_bytes_that_are_not_a_docx_are_a_terminal_invalid_document(
    content: bytes,
) -> None:
    with pytest.raises(DocumentParseError) as raised:
        DoclingDocxParser().parse(content, DOCX, "faux.docx")

    assert raised.value.code == INVALID_DOCUMENT
