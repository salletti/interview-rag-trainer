"""Agrégat ``KnowledgeCorpus`` et entité ``SearchIndex`` (S4 ; knowledge-base.md
§3.2, §4.1 I10 et I11, §5.4, §13 ; ADR 0011).

API figée (``knowledge_base.domain.entities.knowledge_corpus``) :

- ``CORPUS_ID`` : identifiant du singleton V1 ; ``IndexStatus`` (``BUILDING``,
  ``ACTIVE``, ``RETIRED``).
- ``SearchIndex`` (dataclass figée) : ``index_id, profile: EmbeddingProfile,
  chunking_policy: ChunkingPolicy, status, created_at, activated_at, retired_at``
  (``None`` tant que non atteints).
- ``KnowledgeCorpus.empty()`` : corpus sans index (``active_index_id`` ``None``,
  ``version`` 0) ; ``KnowledgeCorpus.restore(corpus_id, active_index_id,
  version, indexes)`` pour la persistance (sans contrôle).
- ``provision_index(*, index_id, profile, chunking_policy, now) -> SearchIndex``
  : crée un index ``BUILDING`` ; ``DuplicateIndexError`` si l'identifiant existe,
  ``IndexAlreadyBuildingError`` si un autre index est déjà ``BUILDING`` (un seul
  index en construction par corpus, revue S9).
- ``activate_index(index_id, *, now, coverage_complete: bool)`` : ``BUILDING`` ->
  ``ACTIVE`` ; l'ancien actif devient ``RETIRED`` (I10). ``coverage_complete``
  est le résultat du contrôle I11 que le use case calcule (S9) ; faux ->
  ``IncompleteCoverageError`` sans aucun effet. Index inconnu ->
  ``UnknownIndexError`` ; index non ``BUILDING`` -> ``InvalidIndexTransitionError``.
- ``retire_index(index_id, *, now)`` : abandon d'un index ``BUILDING`` ; tout
  autre statut -> ``InvalidIndexTransitionError``.
- Lecture : ``corpus_id``, ``active_index_id``, ``version``, ``indexes``
  (tuple, ordre de création), ``get_index(index_id)`` (``UnknownIndexError``),
  ``active_index() -> SearchIndex | None``, ``live_indexes()`` : les index
  ``ACTIVE`` et ``BUILDING`` (ceux que ``RecordExtraction`` doit alimenter).
- Exceptions : ``knowledge_base.domain.exceptions.corpus_errors``
  (``KnowledgeCorpusError`` en base).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from knowledge_base.domain.entities.knowledge_corpus import (
    CORPUS_ID,
    IndexStatus,
    KnowledgeCorpus,
    SearchIndex,
)
from knowledge_base.domain.exceptions.corpus_errors import (
    DuplicateIndexError,
    IncompleteCoverageError,
    IndexAlreadyBuildingError,
    InvalidIndexTransitionError,
    KnowledgeCorpusError,
    UnknownIndexError,
)
from knowledge_base.domain.value_objects.chunking_policy import ChunkingPolicy
from knowledge_base.domain.value_objects.embedding_profile import (
    MULTILINGUAL_E5_BASE,
)

T0 = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(hours=2)
POLICY = ChunkingPolicy(version="chunking-1", tokenizer="test-words-v1", max_tokens=400)
FIRST, SECOND, THIRD = UUID(int=1), UUID(int=2), UUID(int=3)


def provision(
    corpus: KnowledgeCorpus, index_id: UUID, now: datetime = T0
) -> SearchIndex:
    return corpus.provision_index(
        index_id=index_id,
        profile=MULTILINGUAL_E5_BASE,
        chunking_policy=POLICY,
        now=now,
    )


def corpus_with_active(index_id: UUID = FIRST) -> KnowledgeCorpus:
    corpus = KnowledgeCorpus.empty()
    provision(corpus, index_id)
    corpus.activate_index(index_id, now=T1, coverage_complete=True)
    return corpus


def statuses(corpus: KnowledgeCorpus) -> dict[UUID, IndexStatus]:
    return {i.index_id: i.status for i in corpus.indexes}


# --- création -------------------------------------------------------------------------


def test_an_empty_corpus_has_no_index_and_the_singleton_identity() -> None:
    corpus = KnowledgeCorpus.empty()

    assert corpus.corpus_id == CORPUS_ID
    assert corpus.active_index_id is None
    assert corpus.active_index() is None
    assert corpus.indexes == ()
    assert corpus.live_indexes() == ()
    assert corpus.version == 0


def test_a_provisioned_index_is_building_with_its_profile_and_policy() -> None:
    corpus = KnowledgeCorpus.empty()

    index = provision(corpus, FIRST)

    assert index == SearchIndex(
        index_id=FIRST,
        profile=MULTILINGUAL_E5_BASE,
        chunking_policy=POLICY,
        status=IndexStatus.BUILDING,
        created_at=T0,
        activated_at=None,
        retired_at=None,
    )
    assert corpus.get_index(FIRST) == index
    assert corpus.active_index_id is None


def test_provisioning_an_existing_index_id_is_refused_without_effect() -> None:
    corpus = KnowledgeCorpus.empty()
    provision(corpus, FIRST)

    with pytest.raises(DuplicateIndexError):
        provision(corpus, FIRST, now=T1)

    assert len(corpus.indexes) == 1
    assert corpus.get_index(FIRST).created_at == T0


def test_a_second_building_index_is_refused_without_effect() -> None:
    corpus = corpus_with_active(FIRST)
    provision(corpus, SECOND, now=T1)

    with pytest.raises(IndexAlreadyBuildingError) as refused:
        provision(corpus, THIRD, now=T2)

    assert refused.value.index_id == SECOND
    assert statuses(corpus) == {
        FIRST: IndexStatus.ACTIVE,
        SECOND: IndexStatus.BUILDING,
    }
    assert corpus.building_index() == corpus.get_index(SECOND)


def test_a_new_index_can_be_built_once_the_building_one_is_abandoned() -> None:
    corpus = corpus_with_active(FIRST)
    provision(corpus, SECOND, now=T1)
    corpus.retire_index(SECOND, now=T1)

    provision(corpus, THIRD, now=T2)

    assert corpus.building_index() == corpus.get_index(THIRD)


def test_a_new_index_can_be_built_once_the_building_one_is_activated() -> None:
    corpus = corpus_with_active(FIRST)
    provision(corpus, SECOND, now=T1)
    corpus.activate_index(SECOND, now=T1, coverage_complete=True)

    provision(corpus, THIRD, now=T2)

    assert corpus.building_index() == corpus.get_index(THIRD)


def test_a_corpus_without_construction_has_no_building_index() -> None:
    assert corpus_with_active(FIRST).building_index() is None


# --- activation (I10, I11) ------------------------------------------------------------


def test_activating_the_first_index_of_an_empty_corpus_makes_it_active() -> None:
    corpus = KnowledgeCorpus.empty()
    provision(corpus, FIRST)

    corpus.activate_index(FIRST, now=T1, coverage_complete=True)

    active = corpus.active_index()
    assert active is not None
    assert (active.index_id, active.status, active.activated_at) == (
        FIRST,
        IndexStatus.ACTIVE,
        T1,
    )
    assert corpus.active_index_id == FIRST


def test_activating_a_second_index_retires_the_previous_active_one() -> None:
    corpus = corpus_with_active(FIRST)
    provision(corpus, SECOND, now=T1)

    corpus.activate_index(SECOND, now=T2, coverage_complete=True)

    assert statuses(corpus) == {FIRST: IndexStatus.RETIRED, SECOND: IndexStatus.ACTIVE}
    assert corpus.active_index_id == SECOND
    assert corpus.get_index(FIRST).retired_at == T2
    assert corpus.get_index(SECOND).retired_at is None


def test_at_most_one_index_is_active_after_any_sequence_of_activations() -> None:
    corpus = corpus_with_active(FIRST)
    for index_id in (SECOND, THIRD):
        provision(corpus, index_id, now=T1)
        corpus.activate_index(index_id, now=T2, coverage_complete=True)

        active = [i for i in corpus.indexes if i.status is IndexStatus.ACTIVE]
        assert [i.index_id for i in active] == [index_id]
        assert corpus.active_index_id == index_id


def test_an_incomplete_coverage_refuses_the_activation_without_effect() -> None:
    corpus = corpus_with_active(FIRST)
    provision(corpus, SECOND, now=T1)

    with pytest.raises(IncompleteCoverageError):
        corpus.activate_index(SECOND, now=T2, coverage_complete=False)

    assert statuses(corpus) == {FIRST: IndexStatus.ACTIVE, SECOND: IndexStatus.BUILDING}
    assert corpus.active_index_id == FIRST
    assert corpus.get_index(SECOND).activated_at is None


def test_activating_an_unknown_index_is_refused() -> None:
    corpus = KnowledgeCorpus.empty()

    with pytest.raises(UnknownIndexError):
        corpus.activate_index(FIRST, now=T1, coverage_complete=True)


def test_activating_the_already_active_index_is_refused_and_keeps_its_date() -> None:
    corpus = corpus_with_active(FIRST)

    with pytest.raises(InvalidIndexTransitionError):
        corpus.activate_index(FIRST, now=T2, coverage_complete=True)

    assert corpus.get_index(FIRST).activated_at == T1


def test_a_retired_index_cannot_be_reactivated() -> None:
    corpus = corpus_with_active(FIRST)
    provision(corpus, SECOND, now=T1)
    corpus.activate_index(SECOND, now=T2, coverage_complete=True)

    with pytest.raises(InvalidIndexTransitionError):
        corpus.activate_index(FIRST, now=T2, coverage_complete=True)

    assert corpus.active_index_id == SECOND


# --- abandon --------------------------------------------------------------------------


def test_a_building_index_can_be_abandoned() -> None:
    corpus = corpus_with_active(FIRST)
    provision(corpus, SECOND, now=T1)

    corpus.retire_index(SECOND, now=T2)

    assert statuses(corpus) == {FIRST: IndexStatus.ACTIVE, SECOND: IndexStatus.RETIRED}
    assert corpus.get_index(SECOND).retired_at == T2
    assert corpus.active_index_id == FIRST


def test_the_active_index_cannot_be_retired_directly() -> None:
    corpus = corpus_with_active(FIRST)

    with pytest.raises(InvalidIndexTransitionError):
        corpus.retire_index(FIRST, now=T2)

    assert corpus.active_index_id == FIRST
    assert corpus.get_index(FIRST).status is IndexStatus.ACTIVE


def test_retiring_an_unknown_index_is_refused() -> None:
    with pytest.raises(UnknownIndexError):
        KnowledgeCorpus.empty().retire_index(FIRST, now=T1)


# --- lecture --------------------------------------------------------------------------


def test_live_indexes_are_the_active_and_building_ones_in_creation_order() -> None:
    corpus = corpus_with_active(FIRST)
    # Un seul index BUILDING à la fois : THIRD est abandonné avant SECOND.
    provision(corpus, THIRD, now=T1)
    corpus.retire_index(THIRD, now=T1)
    provision(corpus, SECOND, now=T2)

    assert [i.index_id for i in corpus.live_indexes()] == [FIRST, SECOND]


def test_a_retired_index_is_no_longer_live() -> None:
    corpus = corpus_with_active(FIRST)
    provision(corpus, SECOND, now=T1)
    corpus.activate_index(SECOND, now=T2, coverage_complete=True)

    assert [i.index_id for i in corpus.live_indexes()] == [SECOND]


def test_looking_up_an_unknown_index_is_a_domain_error() -> None:
    with pytest.raises(KnowledgeCorpusError):
        KnowledgeCorpus.empty().get_index(FIRST)


def test_a_restored_corpus_is_taken_as_is() -> None:
    source = corpus_with_active(FIRST)
    provision(source, SECOND, now=T1)

    restored = KnowledgeCorpus.restore(
        corpus_id=CORPUS_ID,
        active_index_id=FIRST,
        version=7,
        indexes=source.indexes,
    )

    assert restored.version == 7
    assert restored.active_index_id == FIRST
    assert restored.indexes == source.indexes
