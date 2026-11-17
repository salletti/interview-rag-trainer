"""Environnement Alembic asynchrone (asyncpg).

Stratégie de migration :
- ``alembic upgrade head`` est le **seul** mécanisme de création/évolution du
  schéma. ``Base.metadata.create_all()`` n'est jamais utilisé hors tests.
- Chaque Bounded Context possède son propre ``MetaData`` dans
  ``<contexte>.frameworks.sqlalchemy``, sous un schéma PostgreSQL dédié ;
  Alembic est l'unique endroit autorisé à en importer plusieurs, pour les
  combiner dans ``target_metadata`` (une liste est supportée nativement).
- Seuls les schémas de ces ``MetaData`` sont comparés par l'autogénération
  et ``alembic check`` ; ``public`` ne contient que ``alembic_version``.
- Dans Docker Compose, le service ``migrate`` exécute ``upgrade head`` avant le
  démarrage de l'API.
"""

from __future__ import annotations

import asyncio
from collections.abc import MutableMapping
from logging.config import fileConfig
from typing import Any

from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from knowledge_base.frameworks.sqlalchemy import metadata as knowledge_base_metadata
from runtime.settings import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = [knowledge_base_metadata]
MANAGED_SCHEMAS = frozenset(m.schema for m in target_metadata if m.schema)


def include_name(
    name: str | None, type_: str, parent_names: MutableMapping[Any, str | None]
) -> bool:
    """Limite la comparaison aux schémas des contextes. Le schéma par défaut
    (``name is None``) en est exclu : les tests d'intégration y créent des
    tables de test qui ne relèvent d'aucune migration."""
    if type_ == "schema":
        return name in MANAGED_SCHEMAS
    return True


def _configure(**options: Any) -> None:
    context.configure(
        target_metadata=target_metadata,
        include_schemas=True,
        include_name=include_name,
        # La table de versions reste dans ``public`` (S0 §2).
        version_table_schema=None,
        **options,
    )


database_url = get_settings().database_url


def run_migrations_offline() -> None:
    """Mode hors ligne : génère le SQL sans se connecter."""
    _configure(
        url=database_url,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(database_url, poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
