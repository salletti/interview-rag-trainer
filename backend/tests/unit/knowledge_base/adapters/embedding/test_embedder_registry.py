"""Fournisseur d'embedders par profil (S9 ; ADR 0011 « la recherche charge le
modèle de son profil », reports S4 M3 et S5 : un seul ``Embedder`` par processus
devient un ``Embedder`` par profil).

API figée (``knowledge_base.adapters.gateway.embedding.embedder_registry``) :

- ``EmbedderRegistry(factories: Mapping[EmbeddingProfile, Callable[[],
  TokenCountingEmbedder]])`` implémente ``EmbedderProvider`` :
  ``for_profile(profile)`` appelle la fabrique du profil AU PREMIER USAGE
  seulement (aucune à la construction, aucune pour les autres profils), rend
  ensuite la même instance, et lève ``UnknownEmbeddingProfileError(profile)`` si
  le profil n'est pas configuré. Une fabrique qui rend un embedder d'un AUTRE
  profil que celui demandé lève ``ValueError`` : ses vecteurs iraient dans la
  mauvaise collection.
- ``EmbedderRegistry.lazy_sentence_transformers(profiles, *, cache_dir=None)`` :
  un ``SentenceTransformersEmbedder.lazy`` par profil ; ne charge aucun modèle.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest

from knowledge_base.domain.value_objects.embedding_profile import MULTILINGUAL_E5_BASE
from tests.unit.knowledge_base.use_cases.indexing_fakes import PROFILE
from tests.unit.knowledge_base.use_cases.s9_support import (
    PROFILE_B,
    PROFILE_C,
    ProfileModel,
    contract,
    unknown_profile_error,
)

REGISTRY = "knowledge_base.adapters.gateway.embedding.embedder_registry"


class Factories:
    """Fabriques qui comptent leurs appels."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def make(
        self, profile_name: str, model: ProfileModel
    ) -> Callable[[], ProfileModel]:
        def build() -> ProfileModel:
            self.calls.append(profile_name)
            return model

        return build


def registry(factories: Factories) -> Any:
    cls = contract(REGISTRY, "EmbedderRegistry")
    return cls(
        {
            PROFILE: factories.make("a", ProfileModel(PROFILE)),
            PROFILE_B: factories.make("b", ProfileModel(PROFILE_B)),
        }
    )


def test_no_model_is_built_when_the_registry_is_created() -> None:
    factories = Factories()

    registry(factories)

    assert factories.calls == []


def test_a_profile_is_built_at_its_first_use_and_only_that_one() -> None:
    factories = Factories()
    models = registry(factories)

    first = models.for_profile(PROFILE_B)

    assert factories.calls == ["b"]
    assert first.profile == PROFILE_B


def test_the_same_instance_is_returned_afterwards() -> None:
    factories = Factories()
    models = registry(factories)

    first = models.for_profile(PROFILE)
    second = models.for_profile(PROFILE)

    assert second is first
    assert factories.calls == ["a"]


def test_each_profile_gets_its_own_model() -> None:
    models = registry(Factories())

    a = models.for_profile(PROFILE)
    b = models.for_profile(PROFILE_B)

    assert (a.profile, b.profile) == (PROFILE, PROFILE_B)
    assert a is not b


def test_the_model_exposes_its_token_counter() -> None:
    models = registry(Factories())

    counter = models.for_profile(PROFILE_B).token_counter

    assert counter.count("un deux trois") == 3


def test_an_unknown_profile_is_refused_with_its_description() -> None:
    factories = Factories()
    models = registry(factories)

    with pytest.raises(unknown_profile_error()) as refused:
        models.for_profile(PROFILE_C)

    assert cast(Any, refused.value).profile == PROFILE_C
    assert factories.calls == []


def test_a_factory_returning_another_profile_is_refused() -> None:
    cls = contract(REGISTRY, "EmbedderRegistry")
    wrong = cls({PROFILE: lambda: ProfileModel(PROFILE_B)})

    with pytest.raises(ValueError, match="profil"):
        wrong.for_profile(PROFILE)


def test_the_sentence_transformers_registry_loads_nothing_and_knows_its_profiles() -> (
    None
):
    cls = contract(REGISTRY, "EmbedderRegistry")
    models = cls.lazy_sentence_transformers([MULTILINGUAL_E5_BASE, PROFILE_B])

    model = models.for_profile(MULTILINGUAL_E5_BASE)

    assert model.profile == MULTILINGUAL_E5_BASE
    assert hasattr(model, "token_counter")
    with pytest.raises(unknown_profile_error()):
        models.for_profile(PROFILE_C)
