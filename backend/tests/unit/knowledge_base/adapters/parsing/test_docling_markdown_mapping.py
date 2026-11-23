"""Traduction Docling -> blocs au-delà du contrat figé par
``test_docling_markdown_parser`` : cas rencontrés sur des fiches réelles
(choix de S3, documentés dans le module de l'adapter), puis corrections de la
revue S3 : front-matter strict, guillemets retirés, imbrication profonde."""

from __future__ import annotations

import pytest

from knowledge_base.adapters.gateway.parsing.docling_markdown_parser import (
    DoclingMarkdownParser,
    read_front_matter,
)
from knowledge_base.domain.exceptions.parsing_errors import (
    DOCUMENT_TOO_COMPLEX,
    DocumentParseError,
)
from knowledge_base.domain.value_objects.structured_document import (
    CodeBlock,
    Heading,
    ListBlock,
    Paragraph,
    StructuredDocument,
)


def parse(source: str, filename: str = "fiche.md") -> StructuredDocument:
    return DoclingMarkdownParser().parse(source.encode(), "text/markdown", filename)


def test_inline_formatting_is_glued_back_with_inline_code_in_backticks() -> None:
    document = parse("Utiliser `Depends` avec **soin** et un [lien](http://x).\n")

    assert document.blocks == (
        Paragraph(text="Utiliser `Depends` avec soin et un lien."),
    )


def test_nested_lists_are_flattened_in_order() -> None:
    document = parse("- un\n- deux `x`\n  - imbriqué\n- trois\n")

    assert document.blocks == (
        ListBlock(items=("un", "deux `x`", "imbriqué", "trois")),
    )


def test_a_fence_without_language_gives_a_code_block_without_language() -> None:
    document = parse("```\nsans langage\n```\n")

    assert document.blocks == (CodeBlock(text="sans langage", language=None),)


def test_deep_headings_keep_their_markdown_level() -> None:
    document = parse("# A\n\n## B\n\n### C\n\n###### F\n\ntexte\n")

    assert [b.level for b in document.blocks if isinstance(b, Heading)] == [1, 2, 3, 6]


def test_a_markdown_extension_is_converted_as_markdown() -> None:
    document = parse("# Titre\n\ntexte\n", filename="notes.markdown")

    assert document.blocks == (Heading(level=1, text="Titre"), Paragraph(text="texte"))


def test_an_unclosed_front_matter_is_plain_content() -> None:
    metadata, body = read_front_matter("---\nkind: qa_sheet\n# Titre\n")

    assert metadata == {}
    assert body == "---\nkind: qa_sheet\n# Titre\n"


def test_the_converter_is_reused_across_documents() -> None:
    parser = DoclingMarkdownParser()

    first = parser.parse(b"# A\n\nun\n", "text/markdown", "a.md")
    second = parser.parse(b"# B\n\ndeux\n", "text/markdown", "b.md")

    assert first.blocks[0] == Heading(level=1, text="A")
    assert second.blocks[0] == Heading(level=1, text="B")


# --- front-matter (revue S3) ------------------------------------------------------


def test_quotes_around_a_front_matter_value_are_removed() -> None:
    metadata, _ = read_front_matter(
        '---\nkind: "qa_sheet"\ntitle: \'Fiche : FastAPI\'\nnote: "l\'été"\n---\n# T\n'
    )

    assert metadata == {"kind": "qa_sheet", "title": "Fiche : FastAPI", "note": "l'été"}


def test_unmatched_or_inner_quotes_are_kept() -> None:
    metadata, _ = read_front_matter("---\na: \"ouvert\nb: l'apostrophe\n---\n")

    assert metadata == {"a": '"ouvert', "b": "l'apostrophe"}


def test_blank_lines_and_comments_are_allowed_in_a_front_matter() -> None:
    metadata, body = read_front_matter(
        "---\n# type de fiche\nkind: qa_sheet\n\n  # langue\nlanguage: fr\n---\n# T\n"
    )

    assert metadata == {"kind": "qa_sheet", "language": "fr"}
    assert body == "# T\n"


@pytest.mark.parametrize(
    "block",
    [
        "Une phrase sans deux-points",
        "kind: qa_sheet\nUne phrase sans deux-points",
        "Note importante: deux mots avant les deux-points",
        ": valeur sans clé",
    ],
    ids=["prose", "prose-after-entry", "key-with-space", "no-key"],
)
def test_a_fenced_block_with_a_line_that_is_not_an_entry_is_plain_content(
    block: str,
) -> None:
    text = f"---\n{block}\n---\n# Titre\n"

    assert read_front_matter(text) == ({}, text)


def test_a_rule_followed_by_prose_reaches_docling_untouched() -> None:
    document = parse("---\nUn paragraphe entre deux règles.\n\n---\n\n# Titre\n")

    assert document.metadata == {}
    assert Paragraph(text="Un paragraphe entre deux règles.") in document.blocks
    assert Heading(level=1, text="Titre") in document.blocks


# --- imbrication profonde (revue S3) ------------------------------------------------


def test_a_deep_but_convertible_list_is_flattened_in_order() -> None:
    depth = 300
    source = "".join("  " * level + f"- e{level}\n" for level in range(depth))

    (block,) = parse(source).blocks

    assert block == ListBlock(items=tuple(f"e{level}" for level in range(depth)))


@pytest.mark.parametrize(
    "source",
    [
        "".join("  " * level + "- e\n" for level in range(2000)),
        ">" * 5000 + " citation\n",
    ],
    ids=["nested-lists", "nested-quotes"],
)
def test_a_nesting_too_deep_for_docling_is_a_terminal_too_complex_document(
    source: str,
) -> None:
    with pytest.raises(DocumentParseError) as raised:
        parse(source)

    assert raised.value.code == DOCUMENT_TOO_COMPLEX
