"""Markdown -> ``StructuredDocument`` par Docling (ADR 0008 §2) : couche
anti-corruption, aucun type Docling ne sort de ce module.

- Le front-matter (``---`` / ``clé: valeur`` / ``---``) est lu ici, AVANT
  Docling, et rendu dans ``metadata`` ; Docling ne reçoit que le corps. Le
  bloc n'est un front-matter que si chacune de ses lignes est ``clé: valeur``,
  vide ou commentaire ``#`` : sinon (une règle horizontale suivie de prose,
  par exemple) le document est rendu tel quel à Docling. Les guillemets
  simples ou doubles qui entourent une valeur sont retirés.
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
- Imbrication trop profonde (listes, citations) : Docling échoue sur une
  ``RecursionError`` ; l'échec est déterministe, rendu en
  ``DOCUMENT_TOO_COMPLEX``. Le parcours de l'arbre Docling, lui, est itératif.

Docling est importé à la première conversion, pas à l'import du module : l'API
compose le handler d'extraction sans payer le chargement de Docling.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from io import BytesIO
from pathlib import PurePath
from typing import TYPE_CHECKING, Final

from knowledge_base.adapters.gateway.parsing.plain_text_parser import decode_utf8
from knowledge_base.domain.exceptions.parsing_errors import (
    DOCUMENT_TOO_COMPLEX,
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
_FRONT_MATTER_ENTRY: Final = re.compile(r"\s*(?P<key>[\w.-]+)\s*:(?P<value>.*)")
_QUOTES: Final = ('"', "'")


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
        try:
            blocks = tuple(_blocks(self._convert(body, filename)))
        except RecursionError as error:
            raise _too_complex(filename) from error
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
        except RuntimeError as error:
            # Le pipeline Docling enveloppe toute erreur de son backend dans
            # RuntimeError(« Pipeline … failed ») ; seule la récursion est
            # reconnue comme déterministe, le reste remonte tel quel.
            if isinstance(error.__cause__, RecursionError):
                raise _too_complex(filename) from error
            raise


def _too_complex(filename: str) -> DocumentParseError:
    return DocumentParseError(
        DOCUMENT_TOO_COMPLEX,
        f"{filename} : imbrication trop profonde pour être convertie",
    )


def read_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Métadonnées ``clé: valeur`` d'un front-matter en tête de document, et le
    reste du texte. Sans barrière fermante, ou si une ligne du bloc n'est ni
    ``clé: valeur``, ni vide, ni un commentaire, il n'y a pas de front-matter."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != FRONT_MATTER_FENCE:
        return {}, text
    for end in range(1, len(lines)):
        if lines[end].strip() == FRONT_MATTER_FENCE:
            metadata = _front_matter_entries(lines[1:end])
            if metadata is None:
                return {}, text
            return metadata, "\n".join(lines[end + 1 :])
    return {}, text


def _front_matter_entries(lines: list[str]) -> dict[str, str] | None:
    """Les entrées du bloc ; ``None`` dès qu'une ligne n'en est pas une."""
    metadata: dict[str, str] = {}
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        entry = _FRONT_MATTER_ENTRY.fullmatch(line)
        if entry is None:
            return None
        metadata[entry["key"]] = _unquoted(entry["value"].strip())
    return metadata


def _unquoted(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in _QUOTES:
        return value[1:-1]
    return value


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

    def walk(root: NodeItem) -> Iterator[Block]:
        # Pile d'itérateurs plutôt que récursion : la profondeur des groupes
        # ne dépend que du document.
        pending = [iter(root.children)]
        while pending:
            reference = next(pending[-1], None)
            if reference is None:
                pending.pop()
                continue
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
                pending.append(iter(item.children))

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
        """Éléments d'une liste et de ses sous-listes, dans l'ordre. Chaque
        niveau de la pile énumère soit les éléments d'une liste, soit les
        enfants d'un élément (texte à mise en forme, sous-liste)."""
        pending = [(True, iter(group.children))]
        while pending:
            in_list, references = pending[-1]
            reference = next(references, None)
            if reference is None:
                pending.pop()
                continue
            item = reference.resolve(document)
            if in_list:
                if isinstance(item, TextItem) and item.text.strip():
                    yield item.text.strip()
                pending.append((False, iter(item.children)))
            elif isinstance(item, InlineGroup):
                yield _inline_text(item).strip()
            elif isinstance(item, ListGroup):
                pending.append((True, iter(item.children)))

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
