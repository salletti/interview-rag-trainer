"""``DoclingConversion`` : conversion commune aux parseurs Docling (revue S6,
I1, M1, M2, M7).

- Un verrou par parseur sérialise création du convertisseur et conversions :
  une conversion abandonnée par ``keeping_lease`` (son thread va à son terme)
  ne partage jamais le convertisseur avec la suivante, qui l'attend.
- ``PARTIAL_SUCCESS`` : document accepté, statut et erreurs journalisés.
- ``ConversionError`` -> ``INVALID_DOCUMENT`` avec l'erreur de Docling et sa
  cause première ; une erreur de syntaxe XML (profondeur maximale de lxml) se
  lit « structure invalide ou trop complexe ».

Le convertisseur Docling est remplacé par un double, sauf pour le DOCX
réellement trop imbriqué (backend Word, sans modèle).
"""

from __future__ import annotations

import io
import logging
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import docx
import pytest
from docling.datamodel.base_models import (
    ConversionStatus,
    DoclingComponentType,
    ErrorItem,
)
from docling.exceptions import ConversionError, DocumentLoadError
from docling_core.types.doc import DocItemLabel, DoclingDocument

from knowledge_base.adapters.gateway.parsing.docling_conversion import (
    DoclingConversion,
)
from knowledge_base.adapters.gateway.parsing.docling_docx_parser import (
    DoclingDocxParser,
)
from knowledge_base.domain.exceptions.parsing_errors import (
    INVALID_DOCUMENT,
    DocumentParseError,
)
from tests.office_support import DOCX

if TYPE_CHECKING:
    from docling.document_converter import DocumentConverter


@dataclass
class FakeResult:
    document: DoclingDocument
    status: ConversionStatus = ConversionStatus.SUCCESS
    errors: list[ErrorItem] = field(default_factory=list)


class SlowConverter:
    """Double de ``DocumentConverter`` qui relève le nombre de conversions
    simultanées."""

    def __init__(self) -> None:
        self.running = 0
        self.max_running = 0
        self._count = threading.Lock()

    def convert(self, source: object) -> FakeResult:
        with self._count:
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        time.sleep(0.05)
        with self._count:
            self.running -= 1
        return FakeResult(document=DoclingDocument(name="double"))


def conversion_of(
    converter: object, built: list[object] | None = None
) -> DoclingConversion:
    def build() -> DocumentConverter:
        if built is not None:
            built.append(converter)
        return cast("DocumentConverter", converter)

    return DoclingConversion(".pdf", build)


def test_concurrent_conversions_of_one_parser_never_overlap() -> None:
    converter = SlowConverter()
    built: list[object] = []
    conversion = conversion_of(converter, built)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda n: conversion.convert(b"x", f"{n}.pdf"), range(4)))

    assert converter.max_running == 1
    assert len(built) == 1  # convertisseur créé une seule fois


def test_the_converter_is_built_at_the_first_conversion_only() -> None:
    built: list[object] = []
    conversion = conversion_of(SlowConverter(), built)

    assert built == []
    conversion.convert(b"x", "a.pdf")
    conversion.convert(b"x", "b.pdf")
    assert len(built) == 1


class PartialConverter:
    def convert(self, source: object) -> FakeResult:
        document = DoclingDocument(name="partiel")
        document.add_text(label=DocItemLabel.TEXT, text="Page lue.")
        return FakeResult(
            document=document,
            status=ConversionStatus.PARTIAL_SUCCESS,
            errors=[
                ErrorItem(
                    component_type=DoclingComponentType.PIPELINE,
                    module_name="layout",
                    error_message="page 3 illisible",
                )
            ],
        )


def test_a_partial_conversion_is_kept_and_its_status_and_errors_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        document = conversion_of(PartialConverter()).convert(b"x", "p.pdf")

    assert document.texts[0].text == "Page lue."
    assert "p.pdf : conversion Docling partial_success" in caplog.text
    assert "page 3 illisible" in caplog.text


class FailingConverter:
    def __init__(self, cause: Exception) -> None:
        self._cause = cause

    def convert(self, source: object) -> FakeResult:
        try:
            try:
                raise self._cause
            except Exception as cause:
                raise DocumentLoadError("backend could not load document") from cause
        except DocumentLoadError as load:
            raise ConversionError(
                "Conversion failed for: x with status: failure. Errors: backend "
                "could not load document"
            ) from load


def test_a_conversion_error_gives_docling_errors_and_the_root_cause() -> None:
    conversion = conversion_of(
        FailingConverter(zipfile.BadZipFile("File is not a zip file"))
    )

    with pytest.raises(DocumentParseError) as raised:
        conversion.convert(b"x", "f.docx")

    assert raised.value.code == INVALID_DOCUMENT
    message = raised.value.message
    assert "f.docx" in message
    assert "Errors: backend could not load document" in message
    assert "BadZipFile: File is not a zip file" in message
    assert "illisible ou corrompu" in message
    assert "chiffré" not in message


def test_an_xml_syntax_error_reads_as_too_complex() -> None:
    conversion = conversion_of(FailingConverter(SyntaxError("Excessive depth")))

    with pytest.raises(DocumentParseError) as raised:
        conversion.convert(b"x", "f.docx")

    assert raised.value.code == INVALID_DOCUMENT
    assert "structure invalide ou trop complexe" in raised.value.message
    assert "Excessive depth" in raised.value.message


def test_a_real_docx_nested_too_deep_names_the_xml_depth_error() -> None:
    """400 tableaux imbriqués : lxml refuse la profondeur (256) sans
    ``XML_PARSE_HUGE``."""
    document = docx.Document()
    document.add_paragraph("Début")
    cell = document.add_table(rows=1, cols=1).cell(0, 0)
    for _ in range(400):
        cell = cell.add_table(rows=1, cols=1).cell(0, 0)
    buffer = io.BytesIO()
    document.save(buffer)

    with pytest.raises(DocumentParseError) as raised:
        DoclingDocxParser().parse(buffer.getvalue(), DOCX, "imbrique.docx")

    assert raised.value.code == INVALID_DOCUMENT
    assert "trop complexe" in raised.value.message
    assert "XMLSyntaxError" in raised.value.message
