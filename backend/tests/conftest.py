"""Fixtures communes.

Les tests unitaires n'ouvrent aucune connexion réseau : les hôtes externes
pointent vers le port 1 (jamais écouté, refus immédiat). Les tests marqués
``integration`` lisent la configuration de ``compose.test.yaml`` depuis
l'environnement (voir Makefile, cible ``test-integration``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from runtime.app import create_app
from tests.support import unreachable_settings


@pytest.fixture
def unreachable_app() -> FastAPI:
    return create_app(unreachable_settings())


@pytest.fixture
async def client(unreachable_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Client HTTP in-process avec le ``lifespan`` réellement exécuté."""
    async with (
        unreachable_app.router.lifespan_context(unreachable_app),
        AsyncClient(
            transport=ASGITransport(app=unreachable_app), base_url="http://test"
        ) as http,
    ):
        yield http
