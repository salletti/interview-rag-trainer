"""Markdown -> ``StructuredDocument`` par Docling (ADR 0008 §2) : couche
anti-corruption, aucun type Docling ne sort de ce module.

- Le front-matter (``---`` / ``clé: valeur`` / ``---``) est lu ici, AVANT
  Docling, et rendu dans ``metadata`` ; Docling ne reçoit que le corps.
- Titres : Docling rend le ``#`` en ``TitleItem`` et ``##``… en
  ``SectionHeaderItem(level=n-1)`` ; les niveaux Markdown sont restitués.
- Paragraphe à mise en forme (gras, lien, code en ligne) : Docling le découpe
  en ``InlineGroup`` ; ses morceaux sont recollés, le code en ligne entre
  accents graves. Les retours à la ligne d'un paragraphe deviennent des espaces.
- Code : ``CodeItem`` -> ``CodeBlock`` ; le langage est celui que Docling
  reconnaît (barrière ou détection), en minuscules, ``None`` si inconnu.
- Liste : un ``ListBlock`` par liste de premier niveau, sous-listes aplaties
  dans l'ordre.
- Tableau : une ligne ``| a | b |`` par rangée (en-tête compris).
- Markdown n'a pas de pages : ``page`` vaut toujours ``None``.

Docling est importé à la première conversion, pas à l'import du module : l'API
compose le handler d'extraction sans payer le chargement de Docling.
"""

from __future__ import annotations

from collections.abc import Iterator
from io import BytesIO
from pathlib import PurePath
from typing import TYPE_CHECKING, Final

from knowledge_base.adapters.gateway.parsing.plain_text_parser import decode_utf8
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

if TYPE_CHECKING:
    from docling.document_converter import DocumentConverter
    from docling_core.types.doc import DoclingDocument, NodeItem, TableItem

FRONT_MATTER_FENCE: Final = "---"
MAX_HEADING_LEVEL: Final = 6


class DoclingMarkdownParser:
    def __init__(self) -> None:
        self._converter: DocumentConverter | None = None

    def parse(
        self, content: bytes, media_type: str, filename: str
    ) -> StructuredDocument:
        text = decode_utf8(content, filename).replace("\r\n", "\n")
        metadata, body = read_front_matter(text)
        if not body.strip():
            raise DocumentParseError(
                EMPTY_DOCUMENT, f"{filename} ne contient aucun texte"
            )
        blocks = tuple(_blocks(self._convert(body, filename)))
        if not blocks:
            raise DocumentParseError(
                EMPTY_DOCUMENT, f"{filename} ne contient aucun bloc exploitable"
            )
        return StructuredDocument(blocks=blocks, metadata=metadata)

    def _convert(self, body: str, filename: str) -> DoclingDocument:
        from docling.datamodel.base_models import DocumentStream, InputFormat
        from docling.document_converter import DocumentConverter
        from docling.exceptions import ConversionError

        if self._converter is None:
            self._converter = DocumentConverter(allowed_formats=[InputFormat.MD])
        # Docling choisit le format sur l'extension : le nom transmis est
        # toujours « .md », quel que soit celui de l'import (« .markdown »).
        stream = DocumentStream(
            name=f"{PurePath(filename).stem or 'document'}.md",
            stream=BytesIO(body.encode()),
        )
        try:
            return self._converter.convert(stream).document
        except ConversionError as error:
            raise DocumentParseError(
                INVALID_DOCUMENT, f"Docling n'a pas pu convertir {filename} : {error}"
            ) from error


def read_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Métadonnées ``clé: valeur`` d'un front-matter en tête de document, et le
    reste du texte. Sans barrière fermante, il n'y a pas de front-matter."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != FRONT_MATTER_FENCE:
        return {}, text
    for end in range(1, len(lines)):
        if lines[end].strip() == FRONT_MATTER_FENCE:
            pairs = (line.partition(":") for line in lines[1:end] if ":" in line)
            metadata = {key.strip(): value.strip() for key, _, value in pairs}
            return metadata, "\n".join(lines[end + 1 :])
    return {}, text


# --- traduction DoclingDocument -> blocs ------------------------------------------


def _blocks(document: DoclingDocument) -> Iterator[Block]:
    from docling_core.types.doc import (
        CodeItem,
        GroupItem,
        InlineGroup,
        ListGroup,
        SectionHeaderItem,
        TableItem,
        TextItem,
        TitleItem,
    )

    def walk(node: NodeItem) -> Iterator[Block]:
        for reference in node.children:
            item = reference.resolve(document)
            if isinstance(item, TitleItem):
                yield Heading(level=1, text=item.text)
            elif isinstance(item, SectionHeaderItem):
                yield Heading(
                    level=min(item.level + 1, MAX_HEADING_LEVEL), text=item.text
                )
            elif isinstance(item, CodeItem):
                yield CodeBlock(
                    text=item.text, language=_language(item.code_language.value)
                )
            elif isinstance(item, TableItem):
                yield Table(text=_table_text(item))
            elif isinstance(item, ListGroup):
                yield ListBlock(items=tuple(_list_items(item)))
            elif isinstance(item, InlineGroup):
                yield from _paragraph(_inline_text(item))
            elif isinstance(item, TextItem):
                yield from _paragraph(item.text)
            elif isinstance(item, GroupItem):
                yield from walk(item)

    def _inline_text(group: NodeItem) -> str:
        parts: list[str] = []
        for reference in group.children:
            piece = reference.resolve(document)
            if isinstance(piece, CodeItem):
                parts.append(f"`{piece.text}`")
            elif isinstance(piece, TextItem):
                parts.append(piece.text)
        return "".join(parts)

    def _list_items(group: NodeItem) -> Iterator[str]:
        for reference in group.children:
            item = reference.resolve(document)
            if isinstance(item, TextItem) and item.text.strip():
                yield item.text.strip()
            for child in item.children:
                nested = child.resolve(document)
                if isinstance(nested, InlineGroup):
                    yield _inline_text(nested).strip()
                elif isinstance(nested, ListGroup):
                    yield from _list_items(nested)

    yield from walk(document.body)


def _paragraph(text: str) -> Iterator[Paragraph]:
    if text.strip():
        yield Paragraph(text=text.strip())


def _language(label: str) -> str | None:
    return None if label.lower() == "unknown" else label.lower()


def _table_text(table: TableItem) -> str:
    return "\n".join(
        "| " + " | ".join(cell.text.strip() for cell in row) + " |"
        for row in table.data.grid
    )
