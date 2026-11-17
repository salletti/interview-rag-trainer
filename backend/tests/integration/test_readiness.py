from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.integration


async def test_ready_reports_all_components_ok(integration_client: AsyncClient) -> None:
    response = await integration_client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert {k: v["status"] for k, v in body["checks"].items()} == {
        "postgres": "ok",
        "qdrant": "ok",
        "rabbitmq": "ok",
    }
