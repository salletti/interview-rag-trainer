"""Cycle de vie de la connexion PostgreSQL : moteur asynchrone et sessions.

Aucun modèle métier ici. Chaque Bounded Context possédera son propre
``MetaData`` et ses propres tables ; ce module ne fournit que le moteur
partagé du processus, une fabrique de sessions et la dépendance FastAPI
« une ``AsyncSession`` par requête ».

Règle : une ``AsyncSession`` n'est jamais partagée entre tâches asyncio
concurrentes. La dépendance ``get_session`` en crée une par requête et la
ferme à la fin de celle-ci.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from runtime.settings import Settings

SessionFactory = async_sessionmaker[AsyncSession]


def create_engine(settings: Settings, *, pool_size: int = 5) -> AsyncEngine:
    """Moteur asynchrone du processus (asyncpg). Ne se connecte pas tout de
    suite : la première connexion est ouverte à la première requête.

    ``pool_size`` (défaut SQLAlchemy : 5) est réduit dans les processus
    enfants des workers, qui n'exécutent qu'une tâche à la fois."""
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=pool_size,
        connect_args={"timeout": settings.external_timeout_seconds},
    )


def create_session_factory(engine: AsyncEngine) -> SessionFactory:
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Dépendance FastAPI : une session par requête, fermée en sortie."""
    factory: SessionFactory = request.app.state.session_factory
    async with factory() as session:
        yield session


async def check_connection(engine: AsyncEngine, timeout_seconds: float) -> bool:
    """Vrai si ``SELECT 1`` aboutit dans le délai imparti."""
    try:
        async with asyncio.timeout(timeout_seconds), engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError, TimeoutError):
        return False
    return True
