from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from runtime.database import check_connection, create_engine, create_session_factory
from runtime.settings import Settings

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


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
    la table de version doit exister et pointer sur l'unique tête des
    migrations (``get_current_head`` échoue s'il y a plusieurs têtes)."""
    head = ScriptDirectory.from_config(Config(str(ALEMBIC_INI))).get_current_head()
    assert head is not None
    engine = create_engine(integration_settings)
    try:
        async with engine.connect() as connection:
            version = await connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
        assert version == head
    finally:
        await engine.dispose()
