"""Value Objects de S3 : blocs structurés, chemin, pages, citation, politique,
unité et chunk (knowledge-base.md §3.4, §3.5, §14 ; ADR 0009).

API figée (``knowledge_base.domain.value_objects.*``, stdlib uniquement,
dataclasses figées, égalité par valeur) :

- ``structured_document`` : ``Heading(level 1..6, text, page=None)``,
  ``Paragraph(text, page=None)``, ``CodeBlock(text, language=None, page=None)``,
  ``Table(text, page=None)`` (texte tabulaire, une ligne par rangée),
  ``ListBlock(items: tuple[str, ...], page=None)`` ; ``Block`` = leur union ;
  ``StructuredDocument(blocks: tuple[Block, ...], metadata: Mapping[str, str])``
  (front-matter) ; ``render_blocks(blocks) -> str`` : titre ``#`` x niveau,
  code entre barrières ``` avec le langage, liste ``- item``, blocs séparés
  par une ligne vide.
- ``section_path.SectionPath(parts: tuple[str, ...] = ())`` ; ``str()`` joint
  par `` > ``.
- ``page_span.PageSpan(start, end)`` : deux ``None`` (inconnu) ou
  ``1 <= start <= end``, sinon ``ValueError``.
- ``citation.Citation`` (I13) : ``document_id, document_title, version_id,
  version_number, section_path: SectionPath, page_start, page_end,
  source_filename`` ; titre et nom de fichier non blancs, numéro >= 1.
- ``chunking_policy.ChunkingPolicy(version, max_tokens, overlap_tokens=0,
  fallbacks=...)`` : version non blanche, ``max_tokens > 0``,
  ``0 <= overlap_tokens < max_tokens``.
- ``knowledge_unit.KnowledgeUnit(unit_id, ordinal, unit_type: UnitType,
  question_text, section_path, page_span, token_count, blocks)`` ;
  ``UnitType`` : ``QA``, ``SECTION``, ``DEFINITION`` ; une unité ``QA`` a une
  question, les autres n'en ont pas.
- ``document_chunk.DocumentChunk(chunk_id, unit_id, ordinal_in_unit,
  contextual_text, chunk_hash, token_count, citation)`` : la citation est
  obligatoire (I13).
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from uuid import UUID

import pytest

from knowledge_base.domain.value_objects.chunking_policy import ChunkingPolicy
from knowledge_base.domain.value_objects.citation import Citation
from knowledge_base.domain.value_objects.document_chunk import DocumentChunk
from knowledge_base.domain.value_objects.knowledge_unit import (
    KnowledgeUnit,
    UnitType,
)
from knowledge_base.domain.value_objects.page_span import PageSpan
from knowledge_base.domain.value_objects.section_path import SectionPath
from knowledge_base.domain.value_objects.structured_document import (
    CodeBlock,
    Heading,
    ListBlock,
    Paragraph,
    StructuredDocument,
    Table,
    render_blocks,
)
from tests.unit.knowledge_base.domain.services.support import base_citation

# --- blocs et rendu -----------------------------------------------------------------


def test_render_blocks_separates_blocks_with_a_blank_line() -> None:
    rendered = render_blocks(
        (
            Heading(level=2, text="Dépendances"),
            Paragraph(text="Depends injecte une dépendance."),
            Table(text="| a | b |\n| 1 | 2 |"),
            ListBlock(items=("premier", "second")),
        )
    )

    assert rendered == (
        "## Dépendances\n\n"
        "Depends injecte une dépendance.\n\n"
        "| a | b |\n| 1 | 2 |\n\n"
        "- premier\n- second"
    )


def test_render_blocks_fences_code_with_its_language() -> None:
    rendered = render_blocks(
        (CodeBlock(text="x = 1", language="python"), CodeBlock(text="ls"))
    )

    assert rendered == "```python\nx = 1\n```\n\n```\nls\n```"


def test_render_blocks_of_nothing_is_empty() -> None:
    assert render_blocks(()) == ""


@pytest.mark.parametrize("level", [0, 7, -1])
def test_a_heading_level_outside_1_to_6_is_rejected(level: int) -> None:
    with pytest.raises(ValueError, match="level"):
        Heading(level=level, text="Titre")


def test_blocks_are_immutable_values() -> None:
    block = Paragraph(text="texte", page=2)

    assert block == Paragraph(text="texte", page=2)
    with pytest.raises(FrozenInstanceError):
        block.text = "autre"  # type: ignore[misc]


def test_a_structured_document_carries_its_front_matter() -> None:
    document = StructuredDocument(
        blocks=(Paragraph(text="a"),), metadata={"kind": "qa_sheet"}
    )

    assert document.metadata["kind"] == "qa_sheet"
    assert StructuredDocument(blocks=()).metadata == {}


# --- chemin de sections et pages ----------------------------------------------------


def test_a_section_path_renders_its_parts_separated_by_arrows() -> None:
    assert str(SectionPath(("FastAPI", "Dépendances"))) == "FastAPI > Dépendances"
    assert str(SectionPath()) == ""


def test_a_page_span_accepts_unknown_or_ordered_pages() -> None:
    assert PageSpan(None, None).start is None
    assert (PageSpan(3, 3).start, PageSpan(3, 7).end) == (3, 7)


@pytest.mark.parametrize(
    ("start", "end"), [(0, 1), (5, 2), (None, 3), (3, None), (-1, 2)]
)
def test_an_incoherent_page_span_is_rejected(
    start: int | None, end: int | None
) -> None:
    with pytest.raises(ValueError, match="page"):
        PageSpan(start, end)


# --- citation (I13) -----------------------------------------------------------------


def citation(**overrides: object) -> Citation:
    fields: dict[str, object] = {
        "document_id": UUID(int=1),
        "document_title": "Python avancé",
        "version_id": UUID(int=2),
        "version_number": 1,
        "section_path": SectionPath(("A",)),
        "page_start": 1,
        "page_end": 2,
        "source_filename": "python.pdf",
    }
    fields.update(overrides)
    return Citation(**fields)  # type: ignore[arg-type]


def test_a_complete_citation_is_built() -> None:
    assert citation().source_filename == "python.pdf"


@pytest.mark.parametrize(
    "overrides",
    [
        {"document_title": " "},
        {"source_filename": ""},
        {"version_number": 0},
    ],
    ids=["blank-title", "empty-filename", "version-zero"],
)
def test_an_incomplete_citation_is_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        citation(**overrides)


def test_a_citation_without_pages_is_valid() -> None:
    assert citation(page_start=None, page_end=None).page_start is None


# --- politique ----------------------------------------------------------------------


def test_a_policy_has_no_overlap_by_default() -> None:
    assert ChunkingPolicy(version="v1", max_tokens=512).overlap_tokens == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"version": " ", "max_tokens": 10},
        {"version": "v1", "max_tokens": 0},
        {"version": "v1", "max_tokens": 10, "overlap_tokens": -1},
        {"version": "v1", "max_tokens": 10, "overlap_tokens": 10},
    ],
    ids=["blank-version", "zero-budget", "negative-overlap", "overlap-ge-budget"],
)
def test_an_incoherent_policy_is_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ChunkingPolicy(**kwargs)  # type: ignore[arg-type]


# --- unité et chunk -----------------------------------------------------------------


def unit(unit_type: UnitType, question: str | None) -> KnowledgeUnit:
    return KnowledgeUnit(
        unit_id=UUID(int=5),
        ordinal=0,
        unit_type=unit_type,
        question_text=question,
        section_path=SectionPath(("A",)),
        page_span=PageSpan(None, None),
        token_count=3,
        blocks=(Paragraph(text="un deux trois"),),
    )


def test_unit_types_are_qa_section_and_definition() -> None:
    assert {t.value for t in UnitType} == {"QA", "SECTION", "DEFINITION"}


def test_a_qa_unit_requires_a_question() -> None:
    with pytest.raises(ValueError, match="question"):
        unit(UnitType.QA, None)


@pytest.mark.parametrize("unit_type", [UnitType.SECTION, UnitType.DEFINITION])
def test_only_a_qa_unit_may_carry_a_question(unit_type: UnitType) -> None:
    with pytest.raises(ValueError, match="question"):
        unit(unit_type, "Pourquoi ?")


def test_a_chunk_cannot_be_built_without_a_citation() -> None:
    with pytest.raises(TypeError):
        DocumentChunk(  # type: ignore[call-arg]
            chunk_id=UUID(int=1),
            unit_id=UUID(int=2),
            ordinal_in_unit=0,
            contextual_text="t",
            chunk_hash="h",
            token_count=1,
        )


def test_a_chunk_keeps_its_citation() -> None:
    chunk = DocumentChunk(
        chunk_id=UUID(int=1),
        unit_id=UUID(int=2),
        ordinal_in_unit=0,
        contextual_text="t",
        chunk_hash="h",
        token_count=1,
        citation=base_citation(),
    )

    assert chunk.citation == base_citation()
