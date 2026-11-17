from __future__ import annotations

import platform

from httpx import AsyncClient


async def test_system_info_exposes_only_non_sensitive_data(
    client: AsyncClient,
) -> None:
    response = await client.get("/api/v1/system/info")

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Interview RAG Trainer"
    assert body["environment"] == "test"
    assert body["python_version"] == platform.python_version()
    assert body["bounded_contexts"] == ["interview_training", "assessment"]
    assert body["docs_url"] == "/docs"
    assert set(body) == {
        "name",
        "version",
        "environment",
        "python_version",
        "bounded_contexts",
        "docs_url",
    }
