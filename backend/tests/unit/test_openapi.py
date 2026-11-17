from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from runtime.app import create_app
from tests.support import unreachable_settings


async def test_openapi_served_in_development(client: AsyncClient) -> None:
    assert (await client.get("/openapi.json")).status_code == 200
    assert (await client.get("/docs")).status_code == 200


async def test_openapi_disabled_in_production() -> None:
    app = create_app(unreachable_settings(environment="production"))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as http:
        assert (await http.get("/openapi.json")).status_code == 404
        assert (await http.get("/docs")).status_code == 404
        assert (await http.get("/health/live")).status_code == 200
