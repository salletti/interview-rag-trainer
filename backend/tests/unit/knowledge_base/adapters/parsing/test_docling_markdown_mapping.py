"""Traduction Docling -> blocs au-delà du contrat figé par
``test_docling_markdown_parser`` : cas rencontrés sur des fiches réelles
(choix de S3, documentés dans le module de l'adapter)."""

from __future__ import annotations

import pytest

pytest.importorskip("docling")

from knowledge_base.adapters.gateway.parsing.docling_markdown_parser import (
    DoclingMarkdownParser,
    read_front_matter,
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
