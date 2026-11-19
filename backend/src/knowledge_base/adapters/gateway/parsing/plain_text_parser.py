"""Adapter TXT natif, sans Docling (ADR 0008 §3 ; point O7 tranché en S3).

Un TXT n'a pas de structure : ni titre, ni page, ni métadonnée. Une suite de
lignes non vides forme un bloc : ``CodeBlock`` (indentation commune retirée)
si toutes ses lignes sont indentées d'au moins 4 espaces ou d'une tabulation,
``Paragraph`` sinon.
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterator

from knowledge_base.domain.exceptions.parsing_errors import (
    EMPTY_DOCUMENT,
    INVALID_DOCUMENT,
    DocumentParseError,
)
from knowledge_base.domain.value_objects.structured_document import (
    Block,
    CodeBlock,
    Paragraph,
    StructuredDocument,
)


class PlainTextParser:
    def parse(
        self, content: bytes, media_type: str, filename: str
    ) -> StructuredDocument:
        text = decode_utf8(content, filename)
        blocks = tuple(_blocks(text.replace("\r\n", "\n").replace("\r", "\n")))
        if not blocks:
            raise DocumentParseError(
                EMPTY_DOCUMENT, f"{filename} ne contient aucun texte"
            )
        return StructuredDocument(blocks=blocks)


def decode_utf8(content: bytes, filename: str) -> str:
    """UTF-8, BOM toléré ; tout autre encodage est un contenu illisible."""
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise DocumentParseError(
            INVALID_DOCUMENT,
            f"{filename} n'est pas encodé en UTF-8 (octet {error.start})",
        ) from error


def _blocks(text: str) -> Iterator[Block]:
    run: list[str] = []
    for line in [*text.split("\n"), ""]:
        if line.strip():
            run.append(line.rstrip())
        elif run:
            yield _block(run)
            run = []


def _block(lines: list[str]) -> Block:
    if all(line.startswith(("    ", "\t")) for line in lines):
        return CodeBlock(text=textwrap.dedent("\n".join(lines)))
    return Paragraph(text="\n".join(lines))
