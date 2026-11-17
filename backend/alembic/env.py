"""Environnement Alembic asynchrone (asyncpg).

Stratégie de migration :
- ``alembic upgrade head`` est le **seul** mécanisme de création/évolution du
  schéma. ``Base.metadata.create_all()`` n'est jamais utilisé hors tests.
- Chaque Bounded Context possédera son propre ``MetaData`` dans sa couche
  d'infrastructure ; Alembic est l'unique endroit autorisé à en importer
  plusieurs, pour les combiner dans ``target_metadata`` (une liste est
  supportée nativement par Alembic).
- Dans Docker Compose, le service ``migrate`` exécute ``upgrade head`` avant le
  démarrage de l'API.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from runtime.settings import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Aucune table métier en Phase 1. À compléter avec la liste des MetaData des
# Bounded Contexts qui persistent quelque chose, par exemple :
#     target_metadata = [interview_training_metadata, assessment_metadata]
target_metadata = None

database_url = get_settings().database_url


def run_migrations_offline() -> None:
    """Mode hors ligne : génère le SQL sans se connecter."""
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
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
