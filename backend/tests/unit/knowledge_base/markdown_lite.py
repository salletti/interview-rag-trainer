"""Double de test du port ``DocumentParser`` pour du Markdown simple.

Remplace la frontière Docling (lourde, absente de l'environnement tant que la
dépendance n'est pas ajoutée) dans les tests de pipeline : front-matter
``clé: valeur``, titres ``#``, blocs de code, listes ``- ``, tableaux ``|`` et
paragraphes. Ce n'est PAS l'implémentation de production ; les goldens sont
aussi rejoués avec le vrai ``DoclingMarkdownParser`` quand Docling est présent.
"""

from __future__ import annotations

import re

from knowledge_base.domain.exceptions.parsing_errors import (
    EMPTY_DOCUMENT,
    INVALID_DOCUMENT,
    DocumentParseError,
)
from knowledge_base.domain.value_objects.structured_document import (
    Block,
    CodeBlock,
    Heading,
    ListBlock,
    Paragraph,
    StructuredDocument,
    Table,
)

_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_FENCE = re.compile(r"^```(\w*)\s*$")


class MarkdownLiteParser:
    def parse(
        self, content: bytes, media_type: str, filename: str
    ) -> StructuredDocument:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise DocumentParseError(INVALID_DOCUMENT, str(error)) from error
        lines = text.replace("\r\n", "\n").split("\n")
        metadata, lines = _front_matter(lines)
        blocks = _blocks(lines)
        if not blocks:
            raise DocumentParseError(EMPTY_DOCUMENT, f"{filename} ne contient rien")
        return StructuredDocument(blocks=tuple(blocks), metadata=metadata)


def _front_matter(lines: list[str]) -> tuple[dict[str, str], list[str]]:
    if not lines or lines[0].strip() != "---":
        return {}, lines
    for end in range(1, len(lines)):
        if lines[end].strip() == "---":
            pairs = (line.partition(":") for line in lines[1:end] if ":" in line)
            return {k.strip(): v.strip() for k, _, v in pairs}, lines[end + 1 :]
    return {}, lines


def _blocks(lines: list[str]) -> list[Block]:
    blocks: list[Block] = []
    run: list[str] = []

    def flush() -> None:
        if not run:
            return
        if all(line.startswith("- ") for line in run):
            blocks.append(ListBlock(items=tuple(line[2:] for line in run)))
        elif all(line.startswith("|") for line in run):
            blocks.append(Table(text="\n".join(run)))
        else:
            blocks.append(Paragraph(text="\n".join(run)))
        run.clear()

    index = 0
    while index < len(lines):
        line = lines[index]
        fence = _FENCE.match(line)
        heading = _HEADING.match(line)
        if fence:
            flush()
            end = index + 1
            while end < len(lines) and not lines[end].startswith("```"):
                end += 1
            blocks.append(
                CodeBlock(
                    text="\n".join(lines[index + 1 : end]), language=fence[1] or None
                )
            )
            index = end
        elif heading:
            flush()
            blocks.append(Heading(level=len(heading[1]), text=heading[2]))
        elif line.strip():
            run.append(line.rstrip())
        else:
            flush()
        index += 1
    flush()
    return blocks
