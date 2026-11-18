"""Fabrique publique ``knowledge_base.build_knowledge_search`` (S5 ; 005,
« Surface publique d'un contexte »).

Signature figée, dans le style de ``build_work_handlers`` et
``provision_search_index`` (S4) : ``build_knowledge_search(session_factory:
async_sessionmaker[AsyncSession], qdrant_client: AsyncQdrantClient, settings:
KnowledgeBaseSettings | None = None, *, embedder: Embedder | None = None) ->
KnowledgeSearch``. Sans ``embedder``, le modèle E5 est chargé paresseusement
(cache ``settings.models_cache_dir``), au premier usage. La fabrique ne se
connecte à rien : l'API la compose au démarrage, avant que les services
n'existent. Retourne l'implémentation du contrat (``adapters/controller/
contracts/``), jamais un type interne.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from sqlalchemy.exc import SQLAlchemyError

import knowledge_base
from knowledge_base import KnowledgeBaseSettings
from knowledge_base.adapters.controller import dependencies
from knowledge_base.contracts.providers import KnowledgeSearch
from knowledge_base.contracts.result import KnowledgeQuery
from runtime.database import create_engine, create_session_factory
from runtime.vector_store import create_qdrant_client
from tests.support import unreachable_settings
from tests.unit.knowledge_base.use_cases.search_fakes import RecordingEmbedder


def test_the_root_package_exports_the_factory() -> None:
    assert "build_knowledge_search" in knowledge_base.__all__
    assert knowledge_base.build_knowledge_search is dependencies.build_knowledge_search


async def test_the_factory_builds_a_search_without_connecting_to_anything() -> None:
    settings = unreachable_settings()
    engine = create_engine(settings)
    client = create_qdrant_client(settings)
    try:
        search: KnowledgeSearch = knowledge_base.build_knowledge_search(
            create_session_factory(engine),
            client,
            KnowledgeBaseSettings(_env_file=None),
            embedder=RecordingEmbedder(),
        )

        assert inspect.iscoroutinefunction(search.search)
    finally:
        await client.close()
        await engine.dispose()


async def test_the_default_embedder_is_lazy_so_building_never_loads_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KB_MODELS_CACHE_DIR", str(tmp_path / "absent"))
    settings = unreachable_settings()
    engine = create_engine(settings)
    client = create_qdrant_client(settings)
    try:
        search = knowledge_base.build_knowledge_search(
            create_session_factory(engine), client
        )

        assert inspect.iscoroutinefunction(search.search)
        assert not (tmp_path / "absent").exists()
    finally:
        await client.close()
        await engine.dispose()


async def test_the_search_fails_loudly_when_postgres_is_unreachable() -> None:
    """Pas d'échec silencieux : un résultat vide serait un faux « rien trouvé »."""
    settings = unreachable_settings()
    engine = create_engine(settings)
    client = create_qdrant_client(settings)
    try:
        search = knowledge_base.build_knowledge_search(
            create_session_factory(engine),
            client,
            KnowledgeBaseSettings(_env_file=None),
            embedder=RecordingEmbedder(),
        )

        with pytest.raises((OSError, SQLAlchemyError)):
            await search.search(KnowledgeQuery(text="depends"))
    finally:
        await client.close()
        await engine.dispose()
