"""Règle d'agrégation de ``/health/ready`` : PostgreSQL requis, Qdrant et
RabbitMQ informatifs (docs/decisions/0006-technical-bootstrap.md)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from runtime import database, messaging, vector_store
from runtime.app import create_app
from tests.support import unreachable_settings


async def _ready(
    monkeypatch: pytest.MonkeyPatch,
    *,
    postgres: bool,
    qdrant: bool,
    rabbitmq: bool,
) -> tuple[int, str]:
    async def fake(ok: bool) -> bool:
        return ok

    monkeypatch.setattr(database, "check_connection", lambda *_a, **_k: fake(postgres))
    monkeypatch.setattr(
        vector_store, "check_connection", lambda *_a, **_k: fake(qdrant)
    )
    monkeypatch.setattr(messaging, "check_connection", lambda *_a, **_k: fake(rabbitmq))
    app: FastAPI = create_app(unreachable_settings())
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http,
    ):
        response = await http.get("/health/ready")
    return response.status_code, response.json()["status"]


async def test_all_up_is_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await _ready(monkeypatch, postgres=True, qdrant=True, rabbitmq=True) == (
        200,
        "ready",
    )


async def test_optional_dependency_down_is_degraded_but_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await _ready(monkeypatch, postgres=True, qdrant=False, rabbitmq=True) == (
        200,
        "degraded",
    )
    assert await _ready(monkeypatch, postgres=True, qdrant=True, rabbitmq=False) == (
        200,
        "degraded",
    )


async def test_postgres_down_is_not_ready_even_if_others_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await _ready(monkeypatch, postgres=False, qdrant=True, rabbitmq=True) == (
        503,
        "not_ready",
    )
