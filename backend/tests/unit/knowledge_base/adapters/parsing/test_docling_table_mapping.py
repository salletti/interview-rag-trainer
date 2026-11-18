"""Tableaux dans la traduction Docling -> blocs, pour tous les formats (revue
S6, B1 et M5).

Docling range le contenu d'une cellule mise en forme (code en ligne, gras,
cellule DOCX de plusieurs paragraphes) dans des groupes enfants du
``TableItem`` (``rich_cell_group_*``). Ce contenu est déjà dans les lignes du
``Table`` : il ne doit pas réapparaître en ``Paragraph``. Les légendes et
notes d'un tableau (PDF) restent émises, après lui.

Une rangée tient sur une ligne : les retours à la ligne d'une cellule
deviennent des espaces.
"""

from __future__ import annotations

import io

import docx
from docling_core.types.doc import (
    DocItemLabel,
    DoclingDocument,
    TableCell,
    TableData,
)

from knowledge_base.adapters.gateway.parsing.docling_docx_parser import (
    DoclingDocxParser,
)
from knowledge_base.adapters.gateway.parsing.docling_mapping import (
    structured_document_from_docling,
)
from knowledge_base.adapters.gateway.parsing.docling_markdown_parser import (
    DoclingMarkdownParser,
)
from knowledge_base.domain.value_objects.structured_document import (
    Block,
    Heading,
    Paragraph,
    Table,
)
from tests.office_support import DOCX


def paragraph_texts(blocks: tuple[Block, ...]) -> list[str]:
    return [b.text for b in blocks if isinstance(b, Paragraph)]


def test_a_markdown_cell_with_inline_code_and_bold_is_not_repeated_as_paragraphs() -> (
    None
):
    source = (
        "# Titre\n\n"
        "| Commande | Effet |\n"
        "|---|---|\n"
        "| `git stash` | met de côté **les modifications** |\n"
        "| git log | historique |\n\n"
        "Fin.\n"
    )

    blocks = DoclingMarkdownParser().parse(source.encode(), "text/markdown", "t.md")

    assert blocks.blocks == (
        Heading(level=1, text="Titre"),
        Table(
            text=(
                "| Commande | Effet |\n"
                "| git stash | met de côté les modifications |\n"
                "| git log | historique |"
            )
        ),
        Paragraph(text="Fin."),
    )


def word_document_with_a_two_paragraph_cell() -> bytes:
    document = docx.Document()
    document.add_heading("Cours", 1)
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Commande"
    table.cell(0, 1).text = "Effet"
    table.cell(1, 0).text = "git stash"
    cell = table.cell(1, 1)
    cell.text = "Premier paragraphe"
    cell.add_paragraph("Second paragraphe")
    document.add_paragraph("Fin.")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_a_docx_cell_of_two_paragraphs_stays_in_its_row_on_one_line() -> None:
    blocks = DoclingDocxParser().parse(
        word_document_with_a_two_paragraph_cell(), DOCX, "t.docx"
    )

    (table,) = [b for b in blocks.blocks if isinstance(b, Table)]
    assert table.text == (
        "| Commande | Effet |\n| git stash | Premier paragraphe Second paragraphe |"
    )
    assert paragraph_texts(blocks.blocks) == ["Fin."]


def test_no_paragraph_repeats_the_text_of_a_cell() -> None:
    blocks = (
        DoclingDocxParser()
        .parse(word_document_with_a_two_paragraph_cell(), DOCX, "t.docx")
        .blocks
    )

    (table,) = [b for b in blocks if isinstance(b, Table)]
    for text in paragraph_texts(blocks):
        assert text not in table.text


def hand_built_table_with_caption_and_footnote() -> DoclingDocument:
    """CONSTRUIT À LA MAIN : aucune fixture enregistrée n'a de légende ni de
    note de tableau. Docling PDF les range en enfants du ``TableItem``,
    référencées par ``captions`` / ``footnotes``."""
    document = DoclingDocument(name="legende")
    data = TableData(num_rows=1, num_cols=2)
    for column, text in enumerate(("GET", "oui")):
        data.table_cells.append(
            TableCell(
                text=text,
                start_row_offset_idx=0,
                end_row_offset_idx=1,
                start_col_offset_idx=column,
                end_col_offset_idx=column + 1,
            )
        )
    table = document.add_table(data=data)
    caption = document.add_text(
        label=DocItemLabel.CAPTION, text="Tableau 1 : méthodes", parent=table
    )
    table.captions.append(caption.get_ref())
    footnote = document.add_text(
        label=DocItemLabel.FOOTNOTE, text="Selon la RFC 9110.", parent=table
    )
    table.footnotes.append(footnote.get_ref())
    document.add_text(label=DocItemLabel.TEXT, text="Après le tableau.")
    return document


def test_hand_built_table_captions_and_footnotes_follow_the_table() -> None:
    blocks = structured_document_from_docling(
        hand_built_table_with_caption_and_footnote()
    ).blocks

    assert blocks == (
        Table(text="| GET | oui |"),
        Paragraph(text="Tableau 1 : méthodes"),
        Paragraph(text="Selon la RFC 9110."),
        Paragraph(text="Après le tableau."),
    )
