"""Une ``AsyncSession`` par requête, jamais partagée entre requêtes
concurrentes, et fermée en sortie de requête."""

from __future__ import annotations

import asyncio

from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.app import create_app
from runtime.database import get_session
from tests.support import unreachable_settings


async def test_concurrent_requests_get_distinct_sessions() -> None:
    app: FastAPI = create_app(unreachable_settings())
    sessions: list[AsyncSession] = []  # références conservées : pas de réemploi d'id()
    gate = asyncio.Event()

    @app.get("/_probe")
    async def probe(session: AsyncSession = Depends(get_session)) -> dict[str, bool]:
        sessions.append(session)
        if len(sessions) == 2:
            gate.set()
        # Les deux requêtes se recouvrent : chacune attend l'arrivée de l'autre.
        await asyncio.wait_for(gate.wait(), timeout=2)
        return {"ok": True}

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http,
    ):
        first, second = await asyncio.gather(http.get("/_probe"), http.get("/_probe"))

    assert first.status_code == second.status_code == 200
    assert len(sessions) == 2
    assert sessions[0] is not sessions[1]
