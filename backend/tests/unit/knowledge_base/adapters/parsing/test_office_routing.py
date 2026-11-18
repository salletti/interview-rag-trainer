"""Composition de production : routage par type de média vers les parseurs de
S6 (ADR 0008 ; ``default_parser`` d'``extract_version_handler``).

Le routage se fait sur le TYPE DE MÉDIA (``Document.source.media_type``), pas
sur ``DocumentKind`` (COURSE, QA_SHEET… : sémantique métier, indépendante du
format) ni sur l'extension. Les classes ``DoclingPdfParser.parse`` et
``DoclingDocxParser.parse`` sont remplacées par des doubles : seul l'aiguillage
est vérifié, sans extraction.
"""

from __future__ import annotations

import pytest

from knowledge_base.adapters.controller.tasks.extract_version_handler import (
    default_parser,
)
from knowledge_base.adapters.gateway.parsing.docling_docx_parser import (
    DoclingDocxParser,
)
from knowledge_base.adapters.gateway.parsing.docling_pdf_parser import (
    DoclingPdfParser,
)
from knowledge_base.domain.exceptions.parsing_errors import (
    INVALID_DOCUMENT,
    DocumentParseError,
)
from knowledge_base.domain.value_objects.structured_document import (
    Paragraph,
    StructuredDocument,
)
from tests.office_support import DOCX, PDF

FROM_PDF = StructuredDocument(blocks=(Paragraph(text="vu par le parseur PDF"),))
FROM_DOCX = StructuredDocument(blocks=(Paragraph(text="vu par le parseur DOCX"),))


@pytest.fixture(autouse=True)
def stub_office_parsers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        DoclingPdfParser, "parse", lambda self, content, media_type, filename: FROM_PDF
    )
    monkeypatch.setattr(
        DoclingDocxParser,
        "parse",
        lambda self, content, media_type, filename: FROM_DOCX,
    )


def test_application_pdf_goes_to_the_pdf_parser() -> None:
    assert default_parser().parse(b"%PDF-1.7", PDF, "a.pdf") is FROM_PDF


def test_the_ooxml_word_type_goes_to_the_docx_parser() -> None:
    assert default_parser().parse(b"PK", DOCX, "a.docx") is FROM_DOCX


@pytest.mark.parametrize("media_type", ["APPLICATION/PDF", "application/pdf; x=y"])
def test_pdf_routing_ignores_case_and_parameters(media_type: str) -> None:
    assert default_parser().parse(b"%PDF-1.7", media_type, "a.pdf") is FROM_PDF


@pytest.mark.parametrize("media_type", ["application/msword", "application/zip"])
def test_other_office_types_stay_unsupported(media_type: str) -> None:
    with pytest.raises(DocumentParseError) as raised:
        default_parser().parse(b"x", media_type, "a.doc")

    assert raised.value.code == INVALID_DOCUMENT
