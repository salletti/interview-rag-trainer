"""Surface publique et composition de S9 (005 ; knowledge-base.md §15.3) : ce
que ``runtime`` appelle pour piloter une migration, et ce que le worker sert.

La conception ne prévoit AUCUN endpoint ni commande d'administration pour
provisionner ou activer un index (``runtime.provision`` n'a que le premier
index, au démarrage de la stack). Proposition testée ici, à valider :

- ``python -m runtime.index_migration start <nom>``, ``... activate <index_id>``,
  ``... status`` : commande d'exploitation comme ``runtime.provision`` (pas de
  route HTTP : une bascule est une opération rare, volontaire, à effet de bord
  lourd) ;
- surface publique (``runtime`` ne voit QUE la racine ``knowledge_base``, règle
  d'architecture ``runtime_only_sees_public_surface``) :
  ``knowledge_base.start_index_migration(session_factory, qdrant_client,
  target_name) -> UUID``, ``knowledge_base.activate_search_index(
  session_factory, index_id) -> None`` et ``knowledge_base.
  migration_target_names() -> tuple[str, ...]`` (les noms du catalogue, pour
  l'aide de la commande) ;
- catalogue ``<nom> -> MigrationTarget(profile, chunking_policy)`` dans
  ``frameworks/configuration/embedding_targets.py`` (``CATALOGUE``), rempli par
  le benchmark ; chaque cible respecte son profil.

Composition : le worker d'ingestion sert ``rechunk_version`` et
``drop_index_collection`` sur la file d'ingestion.
"""

from __future__ import annotations

import importlib
import inspect

from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import async_sessionmaker

import knowledge_base
from knowledge_base import KnowledgeBaseSettings
from knowledge_base.adapters.controller.dependencies import build_work_handlers
from knowledge_base.adapters.controller.tasks.registry import HandlerRegistry
from knowledge_base.domain.value_objects.embedding_profile import MULTILINGUAL_E5_BASE
from knowledge_base.frameworks.configuration.chunking import DEFAULT_CHUNKING_POLICY
from tests.unit.knowledge_base.use_cases.s9_support import contract

INGESTION = "knowledge_base.ingestion"


def test_the_public_surface_exposes_the_migration_entry_points() -> None:
    for name in ("start_index_migration", "activate_search_index"):
        assert name in knowledge_base.__all__
        assert inspect.iscoroutinefunction(getattr(knowledge_base, name))
    assert "migration_target_names" in knowledge_base.__all__
    assert "multilingual-e5-base" in knowledge_base.migration_target_names()


def test_the_operations_command_exists() -> None:
    module = importlib.import_module("runtime.index_migration")

    assert callable(module.main)


def test_the_production_worker_serves_the_rechunk_and_the_collection_drop() -> None:
    handlers = build_work_handlers(
        async_sessionmaker(),
        AsyncQdrantClient(url="http://localhost:1", check_compatibility=False),
        KnowledgeBaseSettings(_env_file=None),
    )

    registry = HandlerRegistry(handlers)

    for kind in ("rechunk_version", "drop_index_collection"):
        assert registry.get(kind).queue == INGESTION
        assert registry.max_attempts_by_kind()[kind] >= 1
        assert kind in registry.kinds_by_queue()[INGESTION]


def test_the_catalogue_starts_from_the_current_target() -> None:
    catalogue = contract(
        "knowledge_base.frameworks.configuration.embedding_targets", "CATALOGUE"
    )

    current = catalogue["multilingual-e5-base"]

    assert current.profile == MULTILINGUAL_E5_BASE
    assert current.chunking_policy == DEFAULT_CHUNKING_POLICY


def test_every_catalogued_policy_fits_the_profile_it_is_built_for() -> None:
    catalogue = contract(
        "knowledge_base.frameworks.configuration.embedding_targets", "CATALOGUE"
    )

    assert catalogue
    for name, target in catalogue.items():
        assert target.chunking_policy.tokenizer == target.profile.tokenizer, name
        assert target.chunking_policy.max_tokens < target.profile.max_tokens, name
