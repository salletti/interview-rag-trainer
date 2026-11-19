"""Fixtures d'intégration de Knowledge Base (PostgreSQL et RabbitMQ réels).

Prérequis : migrations appliquées (``alembic upgrade head``), comme le fait
``make test-integration`` avant ``pytest -m integration``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from knowledge_base import WorkHandler
from runtime.database import SessionFactory, create_engine, create_session_factory
from runtime.settings import Settings
from tests.integration.knowledge_base.support import (
    CREATE_EFFECTS,
    Behaviour,
    FailureHook,
    NewHandler,
    delete_queue,
    unique_handler,
)


@pytest.fixture
async def engine(integration_settings: Settings) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(integration_settings)
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> SessionFactory:
    return create_session_factory(engine)


@pytest.fixture(autouse=True)
async def clean_state(session_factory: SessionFactory) -> None:
    """Table de test des effets créée, puis tables vidées avant chaque test."""
    async with session_factory() as session, session.begin():
        await session.execute(text(CREATE_EFFECTS))
        await session.execute(text("TRUNCATE kb_test_effects"))
        await session.execute(text("DELETE FROM knowledge_base.work_items"))


@pytest.fixture
def new_handler(integration_settings: Settings) -> Iterator[NewHandler]:
    """``unique_handler`` ; les files créées sont supprimées en fin de test."""
    created: list[str] = []

    def make(
        behaviour: Behaviour,
        failure_hook: FailureHook | None = None,
        *,
        hookless: bool = False,
        max_attempts: int | None = None,
    ) -> WorkHandler:
        handler = unique_handler(
            behaviour, failure_hook, hookless=hookless, max_attempts=max_attempts
        )
        created.append(handler.queue)
        return handler

    yield make
    for queue in created:
        delete_queue(integration_settings, queue)
