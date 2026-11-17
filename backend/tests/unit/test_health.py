from __future__ import annotations

from httpx import AsyncClient


async def test_live_answers_without_external_dependencies(
    client: AsyncClient,
) -> None:
    response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


async def test_ready_is_503_when_postgres_unreachable(client: AsyncClient) -> None:
    response = await client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["postgres"] == {"status": "unavailable", "required": True}
    assert body["checks"]["qdrant"]["required"] is False
    assert body["checks"]["rabbitmq"]["required"] is False


async def test_ready_never_leaks_connection_details(client: AsyncClient) -> None:
    response = await client.get("/health/ready")

    text = response.text
    forbidden_fragments = (
        "127.0.0.1",
        "unused",
        "amqp://",
        "postgresql",
        "interview_rag_trainer",
    )
    for forbidden in forbidden_fragments:
        assert forbidden not in text
