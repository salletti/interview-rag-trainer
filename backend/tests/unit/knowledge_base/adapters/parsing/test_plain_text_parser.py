"""``PlainTextParser`` : adapter TXT natif, sans Docling (ADR 0008 §3, O7).

API figée :

- ``knowledge_base.domain.providers.document_parser.DocumentParser`` : port,
  ``parse(content: bytes, media_type: str, filename: str) -> StructuredDocument``
  (synchrone : CPU pur) ; erreurs terminales
  ``knowledge_base.domain.exceptions.parsing_errors.DocumentParseError(code,
  message="")`` avec ``code`` ``EMPTY_DOCUMENT`` (aucun contenu) ou
  ``INVALID_DOCUMENT`` (contenu illisible), constantes du même module.
- ``knowledge_base.adapters.gateway.parsing.plain_text_parser.PlainTextParser`` :
  UTF-8 (BOM toléré), fins de ligne normalisées en ``\\n`` ; un bloc par suite
  de lignes non vides : ``CodeBlock`` (sans langage, indentation commune
  retirée) si toutes ses lignes sont indentées d'au moins 4 espaces ou d'une
  tabulation, ``Paragraph`` sinon (retours à la ligne conservés). Jamais de
  titre, ni de page, ni de métadonnée : un TXT n'a pas de structure.
"""

from __future__ import annotations

import pytest

from knowledge_base.adapters.gateway.parsing.plain_text_parser import PlainTextParser
from knowledge_base.domain.exceptions.parsing_errors import (
    EMPTY_DOCUMENT,
    INVALID_DOCUMENT,
    DocumentParseError,
)
from knowledge_base.domain.value_objects.structured_document import (
    CodeBlock,
    Paragraph,
    StructuredDocument,
)

TEXT = "text/plain"


def parse(content: bytes) -> StructuredDocument:
    return PlainTextParser().parse(content, TEXT, "notes.txt")


def test_blank_line_separated_runs_of_lines_become_paragraphs() -> None:
    document = parse(
        "Les coroutines ne bloquent pas la boucle.\nElles cèdent la main.\n\n"
        "Un deuxième paragraphe.\n".encode()
    )

    assert document.blocks == (
        Paragraph(
            text="Les coroutines ne bloquent pas la boucle.\nElles cèdent la main."
        ),
        Paragraph(text="Un deuxième paragraphe."),
    )


def test_an_indented_run_is_a_code_block_without_its_common_indentation() -> None:
    document = parse(
        b"Exemple :\n\n    async def main():\n        await asyncio.sleep(1)\n\nFin.\n"
    )

    assert document.blocks == (
        Paragraph(text="Exemple :"),
        CodeBlock(text="async def main():\n    await asyncio.sleep(1)", language=None),
        Paragraph(text="Fin."),
    )


def test_a_tab_indented_run_is_a_code_block() -> None:
    document = parse(b"\tx = 1\n\ty = 2\n")

    assert document.blocks == (CodeBlock(text="x = 1\ny = 2", language=None),)


def test_a_partly_indented_run_stays_a_paragraph() -> None:
    document = parse(b"Une liste :\n    - un\n    - deux\n")

    assert [type(b) for b in document.blocks] == [Paragraph]


def test_windows_line_endings_and_a_bom_are_normalised() -> None:
    document = parse("﻿Première ligne\r\nDeuxième ligne\r\n\r\nSuite\r\n".encode())

    assert document.blocks == (
        Paragraph(text="Première ligne\nDeuxième ligne"),
        Paragraph(text="Suite"),
    )


def test_a_text_file_has_no_heading_page_or_metadata() -> None:
    document = parse(b"Titre apparent\n\nCorps.\n")

    assert document.metadata == {}
    assert all(getattr(b, "page", None) is None for b in document.blocks)
    assert not any(type(b).__name__ == "Heading" for b in document.blocks)


def test_accents_are_preserved() -> None:
    document = parse("Les décorateurs enveloppent une fonction à l'exécution.".encode())

    assert document.blocks == (
        Paragraph(text="Les décorateurs enveloppent une fonction à l'exécution."),
    )


@pytest.mark.parametrize("content", [b"", b"   \n\n \t\n", b"\xef\xbb\xbf\n"])
def test_a_file_without_content_is_a_terminal_empty_document(content: bytes) -> None:
    with pytest.raises(DocumentParseError) as raised:
        parse(content)

    assert raised.value.code == EMPTY_DOCUMENT == "EMPTY_DOCUMENT"


def test_bytes_that_are_not_utf8_are_a_terminal_invalid_document() -> None:
    with pytest.raises(DocumentParseError) as raised:
        parse(b"caf\xe9 en latin-1")

    assert raised.value.code == INVALID_DOCUMENT == "INVALID_DOCUMENT"
