"""Les tests d'intégration ciblent la stack ``compose.test.yaml``.

Ils lisent la configuration dans l'environnement (POSTGRES_*, QDRANT_*,
RABBITMQ_*), positionné par ``make test-integration``. Sans cet
environnement, ils échoueraient : c'est voulu, un test d'intégration ne doit
jamais passer « par défaut » sans service réel.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from runtime.app import create_app
from runtime.settings import Settings

pytestmark = pytest.mark.integration


@pytest.fixture
def integration_settings() -> Settings:
    return Settings(environment="test", external_timeout_seconds=5.0)


@pytest.fixture
async def integration_client(
    integration_settings: Settings,
) -> AsyncIterator[AsyncClient]:
    app: FastAPI = create_app(integration_settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
    ):
        yield http
