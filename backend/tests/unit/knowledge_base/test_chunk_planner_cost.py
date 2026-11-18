"""Coût de la découpe d'un bloc sans frontière (revue S3).

Avant correction, chaque fragment cherchait son plus long préfixe par
dichotomie sur tout le reste du bloc : découpe quadratique (2 Mo de lettres :
17,9 s mesurées le 2026-11-18, pendant lesquelles le heartbeat garde le lease).
Bonds doublés puis dichotomie : 0,2 s sur la même machine. La borne de 5 s est
volontairement large (CI lente) ; elle échoue nettement sur l'ancien code.

Compteur et politique de production : le coût dépend du compteur réel.
"""

from __future__ import annotations

import time
from uuid import UUID

from knowledge_base.adapters.gateway.embedding.approximate_token_counter import (
    ApproximateTokenCounter,
)
from knowledge_base.domain.services.chunk_planner import (
    CONTINUATION_MARKER,
    ChunkPlanner,
)
from knowledge_base.domain.value_objects.knowledge_unit import KnowledgeUnit, UnitType
from knowledge_base.domain.value_objects.page_span import PageSpan
from knowledge_base.domain.value_objects.section_path import SectionPath
from knowledge_base.domain.value_objects.structured_document import Paragraph
from knowledge_base.frameworks.configuration.chunking import DEFAULT_CHUNKING_POLICY
from tests.unit.knowledge_base.domain.services.support import TITLE, base_citation

COUNTER = ApproximateTokenCounter()
BUDGET = DEFAULT_CHUNKING_POLICY.max_tokens
TIME_LIMIT_SECONDS = 5.0
HEAD = f"{TITLE}\nCours"


def oversized_unit(text: str) -> KnowledgeUnit:
    return KnowledgeUnit(
        unit_id=UUID(int=1),
        ordinal=0,
        unit_type=UnitType.SECTION,
        question_text=None,
        section_path=SectionPath(("Cours",)),
        page_span=PageSpan(None, None),
        token_count=COUNTER.count(text),
        blocks=(Paragraph(text=text),),
    )


def fragment(contextual_text: str) -> str:
    body = contextual_text.removeprefix(f"{HEAD}\n\n")
    return body.removeprefix(f"{CONTINUATION_MARKER}\n")


def test_two_megabytes_without_any_boundary_are_cut_quickly_within_budget() -> None:
    text = "a" * 2_000_000
    planner = ChunkPlanner(COUNTER, DEFAULT_CHUNKING_POLICY)

    started = time.perf_counter()
    chunks = planner.plan(oversized_unit(text), base_citation())
    elapsed = time.perf_counter() - started

    assert elapsed < TIME_LIMIT_SECONDS
    assert all(c.token_count <= BUDGET for c in chunks)
    assert "".join(fragment(c.contextual_text) for c in chunks) == text


def test_each_character_fragment_is_as_long_as_the_budget_allows() -> None:
    # Le gain de coût ne doit pas se payer en fragments plus courts : un
    # caractère de plus ferait dépasser le budget (le marqueur de continuation
    # est réservé pour chaque fragment, premier compris).
    text = "ab," * 20_000
    chunks = ChunkPlanner(COUNTER, DEFAULT_CHUNKING_POLICY).plan(
        oversized_unit(text), base_citation()
    )

    consumed = 0
    for chunk in chunks[:-1]:
        piece = fragment(chunk.contextual_text)
        consumed += len(piece)
        longer = f"{HEAD}\n\n{CONTINUATION_MARKER}\n{piece}{text[consumed]}"
        assert COUNTER.count(longer) > BUDGET
