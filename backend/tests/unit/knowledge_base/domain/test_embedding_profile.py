"""Value Object ``EmbeddingProfile`` et profil initial E5 (S4 ; ADR 0011 §1 et
§6 ; knowledge-base.md §13).

API figée (``knowledge_base.domain.value_objects.embedding_profile``,
stdlib uniquement, dataclass figée, égalité par valeur) :

- ``Distance`` (``StrEnum``) : ``COSINE``, ``DOT``, ``EUCLID``.
- ``EmbeddingProfile(model, revision, dimension, distance, normalize,
  query_prefix, passage_prefix, max_tokens, tokenizer)`` : ``model``,
  ``revision`` et ``tokenizer`` non blancs ; ``dimension > 0`` ;
  ``max_tokens > 0`` ; sinon ``ValueError``.
- ``MULTILINGUAL_E5_BASE`` : le profil initial (révisable, ADR 0011 §6).
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from knowledge_base.domain.value_objects.embedding_profile import (
    MULTILINGUAL_E5_BASE,
    Distance,
    EmbeddingProfile,
)


def test_the_initial_profile_is_multilingual_e5_base() -> None:
    profile = MULTILINGUAL_E5_BASE

    assert profile.model == "intfloat/multilingual-e5-base"
    assert profile.dimension == 768
    assert profile.distance is Distance.COSINE
    assert profile.normalize is True
    assert profile.max_tokens == 512


def test_the_initial_profile_carries_the_e5_prefixes_with_their_trailing_space() -> (
    None
):
    assert MULTILINGUAL_E5_BASE.query_prefix == "query: "
    assert MULTILINGUAL_E5_BASE.passage_prefix == "passage: "


def test_the_initial_profile_pins_a_revision_and_a_tokenizer() -> None:
    assert MULTILINGUAL_E5_BASE.revision.strip() != ""
    assert MULTILINGUAL_E5_BASE.tokenizer.strip() != ""


def test_two_profiles_differing_by_one_field_are_different_values() -> None:
    other = replace(MULTILINGUAL_E5_BASE, revision="autre-revision")

    assert other != MULTILINGUAL_E5_BASE
    assert replace(MULTILINGUAL_E5_BASE) == MULTILINGUAL_E5_BASE
    assert hash(replace(MULTILINGUAL_E5_BASE)) == hash(MULTILINGUAL_E5_BASE)


def test_a_profile_is_immutable() -> None:
    with pytest.raises(FrozenInstanceError):
        MULTILINGUAL_E5_BASE.dimension = 1024  # type: ignore[misc]


@pytest.mark.parametrize("dimension", [0, -768])
def test_a_non_positive_dimension_is_rejected(dimension: int) -> None:
    with pytest.raises(ValueError, match="dimension"):
        replace(MULTILINGUAL_E5_BASE, dimension=dimension)


@pytest.mark.parametrize("max_tokens", [0, -1])
def test_a_non_positive_token_limit_is_rejected(max_tokens: int) -> None:
    with pytest.raises(ValueError, match="max_tokens"):
        replace(MULTILINGUAL_E5_BASE, max_tokens=max_tokens)


@pytest.mark.parametrize("field", ["model", "revision", "tokenizer"])
def test_a_blank_identity_field_is_rejected(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        EmbeddingProfile(**{**vars(MULTILINGUAL_E5_BASE), field: "  "})
