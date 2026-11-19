"""Fabrique publique ``knowledge_base.build_knowledge_search`` (S5 ; 005,
« Transmission d'un contrat public à un autre contexte »).

Signature figée : ``build_knowledge_search(resources: KnowledgeBaseResources)
-> KnowledgeSearch``, celle de l'esquisse de 005. Elle remplace la signature
de S5 (``session_factory, qdrant_client, settings=None, *, embedder=None``),
qui exposait le type interne ``Embedder`` et laissait chaque appel créer son
propre modèle : un consommateur composé par ``runtime`` à côté de la route de
recherche aurait chargé E5 (~1,1 Gio) une seconde fois (revue du commit S5,
I1). Le modèle appartient désormais aux ressources : depuis S9, un
fournisseur de modèles par profil (``KnowledgeBaseResources.embedders``,
paresseux), qui remplace l'unique ``embedder`` de S5 ; la route montée par
``register`` et la fabrique le partagent. La fabrique ne se connecte à rien
et retourne l'implémentation du contrat (``adapters/controller/contracts/``),
jamais un type interne.

La variante interne ``composition.compose_knowledge_search(session_factory,
qdrant_client, settings, *, embedders | embedder, token_counter)`` sert aux
tests qui injectent un double d'embedder.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import SQLAlchemyError

import knowledge_base
from knowledge_base import KnowledgeBaseResources, KnowledgeBaseSettings
from knowledge_base.adapters.controller import composition, dependencies
from knowledge_base.adapters.gateway.embedding import sentence_transformers_embedder
from knowledge_base.contracts.providers import KnowledgeSearch
from knowledge_base.contracts.result import KnowledgeQuery, KnowledgeSearchResult
from knowledge_base.domain.value_objects.embedding_profile import (
    MULTILINGUAL_E5_BASE,
    EmbeddingProfile,
)
from runtime.app import create_app
from runtime.database import create_engine, create_session_factory
from runtime.vector_store import create_qdrant_client
from tests.support import unreachable_settings
from tests.unit.knowledge_base.use_cases.indexing_fakes import single_profile
from tests.unit.knowledge_base.use_cases.search_fakes import RecordingEmbedder


@asynccontextmanager
async def resources(
    settings: KnowledgeBaseSettings | None = None,
) -> AsyncIterator[KnowledgeBaseResources]:
    runtime_settings = unreachable_settings()
    engine = create_engine(runtime_settings)
    client = create_qdrant_client(runtime_settings)
    try:
        yield KnowledgeBaseResources(
            session_factory=create_session_factory(engine),
            settings=settings or KnowledgeBaseSettings(_env_file=None),
            qdrant_client=client,
        )
    finally:
        await client.close()
        await engine.dispose()


def test_the_root_package_exports_the_factory() -> None:
    assert "build_knowledge_search" in knowledge_base.__all__
    assert knowledge_base.build_knowledge_search is dependencies.build_knowledge_search


def test_the_factory_takes_the_resources_of_the_context_only() -> None:
    signature = inspect.signature(knowledge_base.build_knowledge_search)

    assert list(signature.parameters) == ["resources"]


def test_the_embedder_is_not_a_constructor_parameter_of_the_resources() -> None:
    # ``runtime`` construit les ressources sans connaître le type interne.
    parameters = inspect.signature(KnowledgeBaseResources).parameters

    assert list(parameters) == ["session_factory", "settings", "qdrant_client"]


async def test_the_factory_builds_a_search_without_loading_the_model(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "absent"
    settings = KnowledgeBaseSettings(_env_file=None, models_cache_dir=cache)

    async with resources(settings) as current:
        search: KnowledgeSearch = knowledge_base.build_knowledge_search(current)

        assert inspect.iscoroutinefunction(search.search)
        assert not cache.exists()


async def test_the_search_fails_loudly_when_postgres_is_unreachable() -> None:
    """Pas d'échec silencieux : un résultat vide serait un faux « rien trouvé »."""
    async with resources() as current:
        search = composition.compose_knowledge_search(
            current.session_factory,
            current.qdrant_client,
            current.settings,
            embedders=single_profile(RecordingEmbedder()),
        )

        with pytest.raises((OSError, SQLAlchemyError)):
            await search.search(KnowledgeQuery(text="depends"))


# --- un seul modèle pour la route et la fabrique --------------------------------


class FakeTokenizer:
    def encode(self, text: str, *, add_special_tokens: bool) -> list[int]:
        return [0] * len(text.split())


class FakeEncoder:
    tokenizer = FakeTokenizer()

    def encode(
        self, sentences: Sequence[str], *, normalize_embeddings: bool
    ) -> Sequence[Sequence[float]]:
        return [[1.0, 0.0] for _ in sentences]


class EmptySearch:
    async def search(self, query: KnowledgeQuery) -> KnowledgeSearchResult:
        return KnowledgeSearchResult(passages=(), complete=True, index_id=UUID(int=1))


async def test_the_search_route_and_the_public_factory_share_one_encoder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le scénario de ``runtime/README.md`` : l'API sert la route de recherche
    et ``runtime`` compose ``build_knowledge_search`` pour un consommateur, sur
    les mêmes ressources. Un seul encodeur doit être chargé."""
    loads: list[EmbeddingProfile] = []

    def load(profile: EmbeddingProfile, cache_dir: Any) -> FakeEncoder:
        loads.append(profile)
        return FakeEncoder()

    monkeypatch.setattr(sentence_transformers_embedder, "_load", load)
    composed: list[Any] = []

    def spy(*args: Any, **kwargs: Any) -> EmptySearch:
        composed.append(kwargs["embedders"])
        return EmptySearch()

    monkeypatch.setattr(dependencies, "compose_knowledge_search", spy)
    app = create_app(unreachable_settings())

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
    ):
        response = await http.post(
            "/api/v1/knowledge-base/search", json={"text": "depends"}
        )
        assert response.status_code == 200
        knowledge_base.build_knowledge_search(app.state.knowledge_base)

        route_embedders, factory_embedders = composed
        assert route_embedders is factory_embedders
        assert route_embedders is app.state.knowledge_base.embedders
        route_model = route_embedders.for_profile(MULTILINGUAL_E5_BASE)
        await route_model.embed_query("depends")
        factory_embedders.for_profile(MULTILINGUAL_E5_BASE).token_counter.count(
            "depends"
        )

    assert loads == [MULTILINGUAL_E5_BASE]
