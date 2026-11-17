from __future__ import annotations

import pytest
from sqlalchemy import text

from runtime.database import check_connection, create_engine, create_session_factory
from runtime.settings import Settings

pytestmark = pytest.mark.integration


async def test_async_engine_reaches_postgres(integration_settings: Settings) -> None:
    engine = create_engine(integration_settings)
    try:
        assert await check_connection(engine, timeout_seconds=5.0) is True
    finally:
        await engine.dispose()


async def test_session_factory_runs_a_query(integration_settings: Settings) -> None:
    engine = create_engine(integration_settings)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            value = await session.scalar(text("SELECT 40 + 2"))
        assert value == 42
    finally:
        await engine.dispose()


async def test_alembic_head_is_applied(integration_settings: Settings) -> None:
    """``make test-integration`` exécute ``alembic upgrade head`` avant pytest :
    la table de version doit exister et pointer sur la révision initiale."""
    engine = create_engine(integration_settings)
    try:
        async with engine.connect() as connection:
            version = await connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
        assert version == "7c1a0e2b9d41"
    finally:
        await engine.dispose()
