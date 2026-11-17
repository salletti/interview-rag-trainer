"""Le ``lifespan`` ferme le moteur SQLAlchemy et le client Qdrant."""

from __future__ import annotations

import pytest

from runtime import app as app_module
from tests.support import unreachable_settings


class FakeEngine:
    disposed = False

    async def dispose(self) -> None:
        self.disposed = True


class FakeQdrant:
    closed = False

    async def close(self) -> None:
        self.closed = True


async def test_lifespan_disposes_engine_and_closes_qdrant_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, qdrant = FakeEngine(), FakeQdrant()
    monkeypatch.setattr(app_module, "create_engine", lambda _s: engine)
    monkeypatch.setattr(app_module, "create_session_factory", lambda _e: object())
    monkeypatch.setattr(app_module, "create_qdrant_client", lambda _s: qdrant)
    app = app_module.create_app(unreachable_settings())

    async with app.router.lifespan_context(app):
        assert app.state.db_engine is engine
        assert app.state.qdrant_client is qdrant
        assert not engine.disposed and not qdrant.closed

    assert engine.disposed
    assert qdrant.closed
