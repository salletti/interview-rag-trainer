"""Golden files du corpus de fixtures FR/EN (S3, critère d'acceptation ; sert
aussi de base à l'évaluation de S5).

Chaque fixture ``tests/fixtures/knowledge_base/<nom>.md|.txt`` a son attendu
``<nom>.golden.json`` : titre, ``kind``, langue, type de média, politique
(version, ``max_tokens``) puis, par unité dans l'ordre : ``type``,
``question``, ``section_path``, ``chunks`` (nombre) et ``blocks`` : l'empreinte
des blocs de l'unité, un ``[type, cinq premiers mots]`` par bloc (revue S3 :
vérifie que chaque explication reste sous sa question). Le compteur de tokens
est celui des tests (un mot = un token), nommé ``TOKENIZER`` dans l'identité
de la politique.

Le pipeline testé est parseur -> ``KnowledgeUnitSegmenter`` -> ``ChunkPlanner``.
Le parseur Markdown est un double (``MarkdownLiteParser``) ; les mêmes goldens
sont rejoués avec le vrai ``DoclingMarkdownParser`` (dépendance ferme du
backend).

Un golden ne se corrige jamais pour faire passer le code : s'il est faux, on le
dit et on le justifie dans le même changement.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from knowledge_base.adapters.gateway.parsing.docling_markdown_parser import (
    DoclingMarkdownParser,
)
from knowledge_base.adapters.gateway.parsing.plain_text_parser import PlainTextParser
from knowledge_base.domain.providers.document_parser import DocumentParser
from knowledge_base.domain.services.chunk_planner import ChunkPlanner
from knowledge_base.domain.services.knowledge_unit_segmenter import (
    KnowledgeUnitSegmenter,
)
from knowledge_base.domain.value_objects.chunking_policy import ChunkingPolicy
from knowledge_base.domain.value_objects.document_chunk import DocumentChunk
from knowledge_base.domain.value_objects.document_kind import DocumentKind
from knowledge_base.domain.value_objects.knowledge_unit import KnowledgeUnit
from knowledge_base.domain.value_objects.structured_document import (
    Block,
    CodeBlock,
    Heading,
    ListBlock,
    Paragraph,
)
from tests.unit.knowledge_base.domain.services.support import (
    COUNTER,
    DOCUMENT_ID,
    TOKENIZER,
    VERSION_ID,
    base_citation,
)
from tests.unit.knowledge_base.markdown_lite import MarkdownLiteParser

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "knowledge_base"
FINGERPRINT_WORDS = 5


@dataclass(frozen=True)
class Case:
    name: str
    source: Path
    golden: dict[str, Any]


def load_cases() -> list[Case]:
    return [
        Case(
            name=path.name.removesuffix(".golden.json"),
            source=next(
                s
                for s in FIXTURES.iterdir()
                if s.name.startswith(path.name.removesuffix(".golden.json") + ".")
                and not s.name.endswith(".golden.json")
            ),
            golden=json.loads(path.read_text(encoding="utf-8")),
        )
        for path in sorted(FIXTURES.glob("*.golden.json"))
    ]


CASES = load_cases()
MARKDOWN_CASES = [c for c in CASES if c.golden["media_type"] == "text/markdown"]


def run(
    case: Case, parser: DocumentParser
) -> tuple[tuple[KnowledgeUnit, ...], tuple[DocumentChunk, ...]]:
    golden = case.golden
    policy = ChunkingPolicy(
        version=golden["policy"]["version"],
        tokenizer=TOKENIZER,
        max_tokens=golden["policy"]["max_tokens"],
    )
    document = parser.parse(
        case.source.read_bytes(), golden["media_type"], case.source.name
    )
    units = tuple(
        KnowledgeUnitSegmenter(COUNTER, policy).segment(
            document, kind=DocumentKind(golden["kind"]), version_id=VERSION_ID
        )
    )
    planner = ChunkPlanner(COUNTER, policy)
    citation = base_citation()
    chunks = tuple(c for u in units for c in planner.plan(u, citation))
    return units, chunks


def summary(
    units: tuple[KnowledgeUnit, ...], chunks: tuple[DocumentChunk, ...]
) -> list[dict[str, Any]]:
    return [
        {
            "type": unit.unit_type.value,
            "question": unit.question_text,
            "section_path": list(unit.section_path.parts),
            "chunks": sum(1 for c in chunks if c.unit_id == unit.unit_id),
            "blocks": [fingerprint(block) for block in unit.blocks],
        }
        for unit in units
    ]


def fingerprint(block: Block) -> list[str]:
    """``[type, cinq premiers mots]`` : assez pour reconnaître le bloc dans la
    fixture, sans figer son rendu exact (Docling et le double diffèrent sur
    la mise en forme en ligne)."""
    if isinstance(block, Heading):
        text = block.text
    elif isinstance(block, ListBlock):
        text = " ".join(block.items)
    else:
        text = block.text
    return [type(block).__name__, " ".join(text.split()[:FINGERPRINT_WORDS])]


def lite_parser(case: Case) -> DocumentParser:
    if case.golden["media_type"] == "text/plain":
        return PlainTextParser()
    return MarkdownLiteParser()


# --- le corpus lui-même -------------------------------------------------------------


def test_the_corpus_covers_every_document_kind_in_french_and_english() -> None:
    kinds = {c.golden["kind"] for c in CASES}
    languages = {c.golden["language"] for c in CASES}
    media = {c.golden["media_type"] for c in CASES}

    assert kinds == {"QA_SHEET", "COURSE", "REVISION_SHEET", "TECHNICAL"}
    assert languages == {"fr", "en"}
    assert media == {"text/markdown", "text/plain"}


def test_each_fixture_has_exactly_one_golden_file() -> None:
    sources = {
        p.name
        for p in FIXTURES.iterdir()
        if p.suffix in {".md", ".txt"} and not p.name.endswith(".golden.json")
    }

    assert sources == {c.source.name for c in CASES}


# --- goldens ------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_units_and_chunk_counts_match_the_golden_file(case: Case) -> None:
    units, chunks = run(case, lite_parser(case))

    assert summary(units, chunks) == case.golden["units"]


@pytest.mark.parametrize("case", MARKDOWN_CASES, ids=lambda c: c.name)
def test_the_same_golden_holds_with_the_real_docling_parser(case: Case) -> None:
    units, chunks = run(case, DoclingMarkdownParser())

    assert summary(units, chunks) == case.golden["units"]


# --- invariants valables pour tout le corpus ------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_no_chunk_exceeds_the_budget_and_every_chunk_is_fully_cited(case: Case) -> None:
    _, chunks = run(case, lite_parser(case))

    budget = case.golden["policy"]["max_tokens"]
    assert chunks
    for chunk in chunks:
        assert chunk.token_count == COUNTER.count(chunk.contextual_text)
        assert chunk.token_count <= budget
        assert chunk.citation.document_id == DOCUMENT_ID
        assert chunk.citation.document_title
        assert chunk.citation.source_filename
        assert chunk.citation.version_number >= 1


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_every_paragraph_and_code_block_ends_up_in_exactly_one_unit(case: Case) -> None:
    parser = lite_parser(case)
    document = parser.parse(
        case.source.read_bytes(), case.golden["media_type"], case.source.name
    )
    units, _ = run(case, parser)

    expected = [b for b in document.blocks if isinstance(b, Paragraph | CodeBlock)]
    kept = [b for u in units for b in u.blocks if isinstance(b, Paragraph | CodeBlock)]
    assert kept == expected


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_a_second_run_yields_identical_units_chunks_and_identifiers(case: Case) -> None:
    parser = lite_parser(case)

    assert run(case, parser) == run(case, parser)
