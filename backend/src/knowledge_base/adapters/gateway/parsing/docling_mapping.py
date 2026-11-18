"""``DoclingDocument`` -> ``StructuredDocument`` (ADR 0008 §2) : la couche
anti-corruption commune aux parseurs Docling (Markdown, PDF, DOCX). Aucun type
Docling ne sort de ``adapters/gateway/parsing/``.

- Parcours en ordre de lecture de TOUT l'arbre : un nœud, puis ses enfants.
  Docling imbrique le contenu d'un DOCX sous le titre qui le précède ; un
  parcours limité aux groupes n'en verrait que les titres.
- Titres : ``TitleItem`` -> niveau 1, ``SectionHeaderItem(level=n)`` -> niveau
  ``min(n + 1, 6)``. Docling rend ``#`` en titre et ``##`` en section de niveau
  1 : les niveaux Markdown sont restitués, un DOCX garde « Titre » = 1 et
  « Titre 1 » = 2. Un PDF n'a aucune hiérarchie (Docling donne le niveau 1 à
  tout, titre compris) : tous ses titres sont de niveau 2.
- Paragraphe à mise en forme (gras, lien, code en ligne) : Docling le découpe
  en ``InlineGroup`` ; ses morceaux sont recollés, le code en ligne entre
  accents graves.
- Code : ``CodeItem`` -> ``CodeBlock`` ; langage reconnu par Docling, en
  minuscules, ``None`` si inconnu. Le code d'un PDF a perdu ses retours à la
  ligne et son indentation : limite de Docling, non corrigée ici.
- Liste : un ``ListBlock`` par liste de premier niveau, sous-listes aplaties
  dans l'ordre.
- Tableau : une ligne ``| a | b |`` par rangée (en-tête compris).
- ``page`` : première page de provenance du bloc (pour une liste ou un
  paragraphe à mise en forme, celle de son premier morceau) ; ``None`` sans
  provenance (Markdown, DOCX).
- Ignorés : le contenu hors corps (couche ``furniture`` : en-têtes et pieds de
  page) et les images, avec tout ce qu'elles contiennent.

Le mapping ne décide pas du vide : un document sans bloc donne
``StructuredDocument(blocks=())`` ; le parseur choisit l'erreur.

Docling est importé à l'appel, pas à l'import du module : composer un parseur
ne le charge pas.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Final

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
    from docling_core.types.doc import DoclingDocument, NodeItem, TableItem

MAX_HEADING_LEVEL: Final = 6


def structured_document_from_docling(document: DoclingDocument) -> StructuredDocument:
    """Blocs du corps du document, sans métadonnée (un front-matter Markdown
    est lu par son parseur, avant Docling)."""
    return StructuredDocument(blocks=tuple(_blocks(document)))


def _blocks(document: DoclingDocument) -> Iterator[Block]:
    from docling_core.types.doc import (
        CodeItem,
        ContentLayer,
        DocItem,
        InlineGroup,
        ListGroup,
        PictureItem,
        SectionHeaderItem,
        TableItem,
        TextItem,
        TitleItem,
    )

    def page(item: NodeItem) -> int | None:
        """Première page de provenance d'un élément ; pour un groupe (liste,
        texte à mise en forme), qui n'en a pas, celle de son premier
        descendant qui en a une."""
        pending = [item]
        while pending:
            node = pending.pop()
            if isinstance(node, DocItem):
                if node.prov:
                    return node.prov[0].page_no
                if node is item:
                    return None
            pending.extend(
                reference.resolve(document) for reference in reversed(node.children)
            )
        return None

    def inline_text(group: NodeItem) -> str:
        parts: list[str] = []
        for reference in group.children:
            piece = reference.resolve(document)
            if isinstance(piece, CodeItem):
                parts.append(f"`{piece.text}`")
            elif isinstance(piece, TextItem):
                parts.append(piece.text)
        return "".join(parts)

    def list_items(group: NodeItem) -> Iterator[str]:
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
                yield inline_text(item).strip()
            elif isinstance(item, ListGroup):
                pending.append((True, iter(item.children)))

    # Pile d'itérateurs plutôt que récursion : la profondeur de l'arbre ne
    # dépend que du document.
    pending = [iter(document.body.children)]
    while pending:
        reference = next(pending[-1], None)
        if reference is None:
            pending.pop()
            continue
        item = reference.resolve(document)
        if item.content_layer != ContentLayer.BODY or isinstance(item, PictureItem):
            continue
        if isinstance(item, ListGroup):
            yield ListBlock(items=tuple(list_items(item)), page=page(item))
            continue
        if isinstance(item, InlineGroup):
            yield from _paragraph(inline_text(item), page(item))
            continue
        if isinstance(item, TitleItem):
            yield Heading(level=1, text=item.text, page=page(item))
        elif isinstance(item, SectionHeaderItem):
            yield Heading(
                level=min(item.level + 1, MAX_HEADING_LEVEL),
                text=item.text,
                page=page(item),
            )
        elif isinstance(item, CodeItem):
            yield CodeBlock(
                text=item.text,
                language=_language(item.code_language.value),
                page=page(item),
            )
        elif isinstance(item, TableItem):
            yield Table(text=_table_text(item), page=page(item))
        elif isinstance(item, TextItem):
            yield from _paragraph(item.text, page(item))
        pending.append(iter(item.children))


def _paragraph(text: str, page: int | None) -> Iterator[Paragraph]:
    if text.strip():
        yield Paragraph(text=text.strip(), page=page)


def _language(label: str) -> str | None:
    return None if label.lower() == "unknown" else label.lower()


def _table_text(table: TableItem) -> str:
    return "\n".join(
        "| " + " | ".join(cell.text.strip() for cell in row) + " |"
        for row in table.data.grid
    )
