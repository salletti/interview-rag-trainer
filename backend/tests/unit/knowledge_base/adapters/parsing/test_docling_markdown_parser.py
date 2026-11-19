"""``DoclingMarkdownParser`` : Markdown -> ``StructuredDocument`` (ADR 0008 §2).

``docling`` est une dépendance ferme du backend : importé directement, son
absence fait échouer le module au lieu de l'ignorer.

API figée (``knowledge_base.adapters.gateway.parsing.docling_markdown_parser``) :
``DoclingMarkdownParser().parse(content, media_type, filename)``, port
``DocumentParser`` ; mêmes erreurs que ``PlainTextParser``.

Contrat de traduction (couche anti-corruption : aucun type Docling en sortie) :

- niveaux de titres Markdown conservés (``#`` -> 1, ``##`` -> 2…) ;
- bloc de code ``Fence`` -> ``CodeBlock(text sans barrières, language)`` ;
- liste -> ``ListBlock(items)`` ; tableau -> ``Table`` (toutes les cellules) ;
- front-matter YAML simple (``clé: valeur``) lu AVANT Docling, rendu dans
  ``metadata`` (clés/valeurs ``str``), jamais en blocs ;
- Markdown n'a pas de pages : ``page`` vaut ``None``.
"""

from __future__ import annotations

import pytest

from knowledge_base.adapters.gateway.parsing.docling_markdown_parser import (
    DoclingMarkdownParser,
)
from knowledge_base.domain.exceptions.parsing_errors import (
    EMPTY_DOCUMENT,
    INVALID_DOCUMENT,
    DocumentParseError,
)
from knowledge_base.domain.value_objects.structured_document import (
    CodeBlock,
    Heading,
    ListBlock,
    Paragraph,
    StructuredDocument,
    Table,
)

MARKDOWN = "text/markdown"


def parse(source: str | bytes) -> StructuredDocument:
    content = source.encode() if isinstance(source, str) else source
    return DoclingMarkdownParser().parse(content, MARKDOWN, "fiche.md")


def test_headings_paragraphs_and_code_keep_their_order_and_levels() -> None:
    document = parse(
        "# Fiche FastAPI\n\n"
        "## Question : Qu'est-ce que Depends ?\n\n"
        "Depends déclare une dépendance injectée.\n\n"
        "```python\ndef endpoint(db=Depends(get_db)):\n    return db\n```\n\n"
        "### Explication\n\n"
        "FastAPI résout la dépendance à chaque requête.\n"
    )

    assert document.blocks == (
        Heading(level=1, text="Fiche FastAPI"),
        Heading(level=2, text="Question : Qu'est-ce que Depends ?"),
        Paragraph(text="Depends déclare une dépendance injectée."),
        CodeBlock(
            text="def endpoint(db=Depends(get_db)):\n    return db", language="python"
        ),
        Heading(level=3, text="Explication"),
        Paragraph(text="FastAPI résout la dépendance à chaque requête."),
    )


def test_a_list_becomes_a_list_block() -> None:
    document = parse("# Liste\n\n- un\n- deux\n- trois\n")

    assert document.blocks[1] == ListBlock(items=("un", "deux", "trois"))


def test_a_table_keeps_all_its_cells_in_one_block() -> None:
    document = parse("# T\n\n| clé | valeur |\n|---|---|\n| a | 1 |\n| b | 2 |\n")

    tables = [b for b in document.blocks if isinstance(b, Table)]
    assert len(tables) == 1
    for cell in ("clé", "valeur", "a", "1", "b", "2"):
        assert cell in tables[0].text


def test_front_matter_goes_to_the_metadata_and_never_to_the_blocks() -> None:
    document = parse("---\nkind: qa_sheet\nlanguage: fr\n---\n\n# Fiche\n\nContenu.\n")

    assert document.metadata == {"kind": "qa_sheet", "language": "fr"}
    assert document.blocks == (
        Heading(level=1, text="Fiche"),
        Paragraph(text="Contenu."),
    )


def test_a_document_without_front_matter_has_no_metadata() -> None:
    assert parse("# Fiche\n\nContenu.\n").metadata == {}


def test_nothing_returned_leaks_a_docling_type() -> None:
    document = parse("# Fiche\n\ntexte\n\n```python\nx = 1\n```\n\n- a\n")

    assert type(document).__module__.startswith("knowledge_base")
    assert all(type(b).__module__.startswith("knowledge_base") for b in document.blocks)


def test_markdown_blocks_have_no_page() -> None:
    document = parse("# Fiche\n\ntexte\n")

    assert all(b.page is None for b in document.blocks)


@pytest.mark.parametrize("source", ["", "   \n\n\t\n", "---\nkind: qa_sheet\n---\n"])
def test_a_markdown_without_content_is_a_terminal_empty_document(source: str) -> None:
    with pytest.raises(DocumentParseError) as raised:
        parse(source)

    assert raised.value.code == EMPTY_DOCUMENT


def test_bytes_that_are_not_utf8_are_a_terminal_invalid_document() -> None:
    with pytest.raises(DocumentParseError) as raised:
        parse(b"# Caf\xe9\n\ntexte\n")

    assert raised.value.code == INVALID_DOCUMENT
