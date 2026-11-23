"""Les tests d'intégration ciblent la stack ``compose.test.yaml``.

Ils lisent la configuration dans l'environnement (POSTGRES_*, QDRANT_*,
RABBITMQ_*), positionné par ``make test-integration``. Sans cet
environnement, ils échoueraient : c'est voulu, un test d'intégration ne doit
jamais passer « par défaut » sans service réel.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from runtime.app import create_app
from runtime.settings import Settings

pytestmark = pytest.mark.integration

# Ports de la stack de développement (compose.yaml) : les fixtures
# d'intégration vident des tables, elles ne doivent jamais les viser.
_DEV_PORTS = {"POSTGRES_PORT": "5440"}


@pytest.fixture(scope="session", autouse=True)
def _refuse_non_test_environment() -> None:
    """Arrête la session si l'environnement n'est pas celui de ``compose.test.yaml``.

    Sans ``make test-integration``, les réglages par défaut visent la base de
    développement, que les fixtures de nettoyage videraient (incident S6 du
    2026-11-18). Ce garde-fou s'exécute avant toute fixture de fonction.
    """
    if os.environ.get("ENVIRONMENT") != "test":
        pytest.exit(
            "tests d'intégration refusés : ENVIRONMENT != 'test' "
            "(lancer `make test-integration`)",
            returncode=2,
        )
    for name, dev_value in _DEV_PORTS.items():
        if os.environ.get(name, dev_value) == dev_value:
            pytest.exit(
                f"tests d'intégration refusés : {name} vise la stack de développement",
                returncode=2,
            )


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
