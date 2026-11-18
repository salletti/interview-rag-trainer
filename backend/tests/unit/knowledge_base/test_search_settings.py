"""Réglages de la recherche (S5 ; knowledge-base.md §12, O12).

Champs figés de ``KnowledgeBaseSettings`` (préfixe ``KB_``) :

- ``search_oversampling_factor`` : ``int >= 1``, défaut 2 (O12, Proposed) ;
- ``search_complement_passes`` : ``int >= 0``, défaut 1 (O12, Proposed) ;
- ``max_passage_tokens`` : ``int > 0``, budget d'un passage (small-to-big),
  « à mesurer » (O12) : le défaut n'est pas figé, mais il doit au moins
  contenir un chunk entier de la politique de production, sans quoi le meilleur
  chunk d'une unité ne tiendrait jamais dans sa fenêtre.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from knowledge_base import KnowledgeBaseSettings
from knowledge_base.frameworks.configuration.chunking import DEFAULT_CHUNKING_POLICY


def make(**overrides: int) -> KnowledgeBaseSettings:
    return KnowledgeBaseSettings(_env_file=None, **overrides)  # type: ignore[arg-type]


def test_the_search_defaults_are_the_values_proposed_by_o12() -> None:
    settings = make()

    assert settings.search_oversampling_factor == 2
    assert settings.search_complement_passes == 1


def test_the_default_passage_budget_holds_at_least_one_whole_chunk() -> None:
    assert make().max_passage_tokens >= DEFAULT_CHUNKING_POLICY.max_tokens


def test_the_search_settings_are_read_from_kb_prefixed_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KB_SEARCH_OVERSAMPLING_FACTOR", "3")
    monkeypatch.setenv("KB_SEARCH_COMPLEMENT_PASSES", "2")
    monkeypatch.setenv("KB_MAX_PASSAGE_TOKENS", "1234")

    settings = make()

    assert settings.search_oversampling_factor == 3
    assert settings.search_complement_passes == 2
    assert settings.max_passage_tokens == 1234


def test_zero_complement_passes_is_allowed() -> None:
    assert make(search_complement_passes=0).search_complement_passes == 0


@pytest.mark.parametrize("field", ["search_oversampling_factor", "max_passage_tokens"])
def test_a_non_positive_factor_or_budget_is_refused(field: str) -> None:
    with pytest.raises(ValidationError):
        make(**{field: 0})


def test_a_negative_number_of_complement_passes_is_refused() -> None:
    with pytest.raises(ValidationError):
        make(search_complement_passes=-1)
