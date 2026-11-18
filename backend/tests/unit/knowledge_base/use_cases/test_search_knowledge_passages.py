"""Reconstruction small-to-big d'un passage (S5 ; knowledge-base.md §12 étape 6 ;
ADR 0009 §6 ; O12).

- Une unité qui tient dans ``max_passage_tokens`` (``token_count`` de l'unité
  <= budget) est rendue COMPLÈTE : question puis blocs, ``truncated=False``.
- Sinon, une fenêtre de chunks contiguë qui contient le meilleur chunk trouvé,
  dont la somme des ``token_count`` ne dépasse pas le budget, dans l'ordre de
  lecture, et qui garde la question racine, ``truncated=True``.

Hypothèses (à confirmer par l'implémentation) : la question racine est dans le
``text`` du passage (en tête pour l'unité complète ; portée par l'en-tête
contextuel des chunks pour une fenêtre, comme le fait ``ChunkPlanner`` S3) ; le
budget d'une unité complète est mesuré sur ``PublishedUnit.token_count``.

Non figé : la fenêtre quand plusieurs chunks trouvés sont éloignés, et le
contenu de ``matched_chunk_ids`` quand certains ne tiennent pas dans la fenêtre.
"""

from __future__ import annotations

from collections.abc import Sequence

from knowledge_base.domain.value_objects.chunk_hit import ChunkHit
from knowledge_base.domain.value_objects.knowledge_unit import UnitType
from knowledge_base.domain.value_objects.published_unit import PublishedUnit
from knowledge_base.use_cases.search_knowledge.search_knowledge_command import (
    SearchKnowledge,
)
from knowledge_base.use_cases.search_knowledge.search_knowledge_result import (
    SearchKnowledgeResult,
)
from tests.unit.knowledge_base.use_cases.search_fakes import (
    chunk_uid,
    hit,
    make_rig,
    whole_unit,
)

QUESTION = "Comment fonctionne le cycle de vie d'une application ?"


def marker(unit: int, rank: int) -> str:
    return f"<BODY-{unit}-{rank}>"


async def search(
    units: Sequence[PublishedUnit], ranking: Sequence[ChunkHit], budget: int
) -> SearchKnowledgeResult:
    rig = make_rig(ranking=ranking, published=units, max_passage_tokens=budget)
    return await rig.handler.handle(SearchKnowledge(text="cycle de vie"))


# --- unité complète -----------------------------------------------------------


async def test_a_unit_within_the_budget_is_whole_with_question_then_answer() -> None:
    unit = whole_unit(1, question=QUESTION, body="Le lifespan ouvre puis ferme.")

    result = await search([unit], [hit(1, 0.9)], budget=100)

    (passage,) = result.passages
    assert passage.truncated is False
    assert QUESTION in passage.text
    assert "Le lifespan ouvre puis ferme." in passage.text
    assert passage.text.index(QUESTION) < passage.text.index("Le lifespan")


async def test_a_whole_unit_returns_all_its_blocks_even_when_one_chunk_matched() -> (
    None
):
    body = "Réponse.\n\n```python\nasync def lifespan(app): ...\n```\n\nExplication."
    unit = whole_unit(1, question=QUESTION, body=body, token_count=60, chunk_count=3)

    result = await search([unit], [hit(1, 0.9, rank=1)], budget=100)

    (passage,) = result.passages
    assert body in passage.text
    assert passage.matched_chunk_ids == (chunk_uid(1, 1),)


async def test_a_unit_exactly_at_the_budget_is_not_truncated() -> None:
    unit = whole_unit(1, token_count=100, chunk_count=2)

    result = await search([unit], [hit(1, 0.9)], budget=100)

    assert result.passages[0].truncated is False


async def test_a_unit_one_token_over_the_budget_is_truncated() -> None:
    unit = whole_unit(1, token_count=101, chunk_count=3)

    result = await search([unit], [hit(1, 0.9, rank=1)], budget=100)

    assert result.passages[0].truncated is True


async def test_a_unit_without_question_is_returned_as_its_blocks_only() -> None:
    unit = whole_unit(1, body="Un cours sur asyncio.", section=True)
    assert unit.unit_type is UnitType.SECTION
    result = await search([unit], [hit(1, 0.9)], budget=100)

    (passage,) = result.passages
    assert passage.question is None
    assert passage.text == "Un cours sur asyncio."
    assert passage.truncated is False


# --- fenêtre bornée -----------------------------------------------------------


async def test_a_unit_over_the_budget_gives_a_window_around_the_matched_chunk() -> None:
    unit = whole_unit(1, question=QUESTION, token_count=240, chunk_count=6)

    result = await search([unit], [hit(1, 0.9, rank=4)], budget=100)

    (passage,) = result.passages
    assert passage.truncated is True
    assert marker(1, 4) in passage.text
    # Le budget (100) ne laisse pas plus de deux chunks de 40 : les chunks 0 à 2
    # sont trop loin du chunk trouvé pour figurer dans la fenêtre.
    for far in (0, 1, 2):
        assert marker(1, far) not in passage.text


async def test_the_window_never_exceeds_the_passage_budget() -> None:
    unit = whole_unit(1, question=QUESTION, token_count=240, chunk_count=6)

    result = await search([unit], [hit(1, 0.9, rank=3)], budget=100)

    text = result.passages[0].text
    kept = [c for c in unit.chunks if marker(1, c.ordinal_in_unit) in text]
    assert kept
    assert sum(c.token_count for c in kept) <= 100


async def test_the_window_keeps_the_root_question_and_the_matched_chunk() -> None:
    unit = whole_unit(1, question=QUESTION, token_count=240, chunk_count=6)

    result = await search([unit], [hit(1, 0.9, rank=5)], budget=100)

    (passage,) = result.passages
    assert passage.question == QUESTION
    assert QUESTION in passage.text
    assert marker(1, 5) in passage.text


async def test_the_window_keeps_the_reading_order_of_the_chunks() -> None:
    unit = whole_unit(1, question=QUESTION, token_count=240, chunk_count=6)

    result = await search(
        [unit], [hit(1, 0.9, rank=3), hit(1, 0.8, rank=2)], budget=120
    )

    text = result.passages[0].text
    assert text.index(marker(1, 2)) < text.index(marker(1, 3))


async def test_the_best_matched_chunk_is_kept_when_the_budget_cannot_hold_all() -> None:
    unit = whole_unit(1, question=QUESTION, token_count=240, chunk_count=4)
    # Deux chunks trouvés de 60 tokens chacun, budget de 100 : un seul tient.
    ranking = [hit(1, 0.9, rank=3), hit(1, 0.6, rank=0)]

    result = await search([unit], ranking, budget=100)

    (passage,) = result.passages
    assert marker(1, 3) in passage.text
    assert marker(1, 0) not in passage.text
    assert passage.truncated is True


async def test_the_matched_chunks_are_reported_even_for_a_truncated_passage() -> None:
    unit = whole_unit(1, question=QUESTION, token_count=240, chunk_count=6)

    result = await search([unit], [hit(1, 0.9, rank=4)], budget=100)

    assert chunk_uid(1, 4) in result.passages[0].matched_chunk_ids


async def test_the_budget_is_applied_to_each_passage_independently() -> None:
    long = whole_unit(1, question=QUESTION, token_count=240, chunk_count=6)
    short = whole_unit(2, question="Une courte question ?", token_count=30)

    result = await search([long, short], [hit(1, 0.9, rank=4), hit(2, 0.8)], budget=100)

    by_unit = {p.unit_id: p for p in result.passages}
    assert by_unit[long.unit_id].truncated is True
    assert by_unit[short.unit_id].truncated is False
