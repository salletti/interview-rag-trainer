"""Route de recherche : traduction des refus en HTTP (S5 ; précision S5 de
knowledge-base.md §14). Complète ``test_search_routes_http``."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from knowledge_base.adapters.controller.rest.documents_router import PREFIX
from knowledge_base.adapters.controller.rest.search_router import add_search_routes
from knowledge_base.adapters.controller.rest.search_schemas import MAX_LIMIT
from knowledge_base.contracts.providers import (
    KnowledgeSearchUnavailableError,
    UnavailabilityReason,
)
from knowledge_base.contracts.result import KnowledgeQuery, KnowledgeSearchResult


class FailingSearch:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.queries: list[KnowledgeQuery] = []

    async def search(self, query: KnowledgeQuery) -> KnowledgeSearchResult:
        self.queries.append(query)
        raise self.error


def client_for(search: FailingSearch) -> AsyncClient:
    app = FastAPI()
    add_search_routes(app.router, knowledge_search=lambda: search)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.parametrize("reason", list(UnavailabilityReason))
async def test_an_unavailable_search_is_a_503_with_its_code(
    reason: UnavailabilityReason,
) -> None:
    search = FailingSearch(KnowledgeSearchUnavailableError(reason, "pas d'index"))

    async with client_for(search) as http:
        response = await http.post(f"{PREFIX}/search", json={"text": "Depends"})

    assert response.status_code == 503
    assert response.json() == {"code": reason.value, "message": "pas d'index"}


async def test_an_infrastructure_failure_is_not_turned_into_an_empty_result() -> None:
    search = FailingSearch(OSError("connexion refusée"))

    async with client_for(search) as http:
        with pytest.raises(OSError):
            await http.post(f"{PREFIX}/search", json={"text": "Depends"})


@pytest.mark.parametrize(
    "body",
    [
        {"text": "x", "limit": MAX_LIMIT + 1},
        {"text": "x", "kinds": ["POEM"]},
        {"text": "x", "unknown": 1},
    ],
    ids=["limit-cap", "unknown-kind", "unknown-field"],
)
async def test_an_out_of_contract_body_is_a_422(body: dict[str, object]) -> None:
    search = FailingSearch(AssertionError("jamais appelée"))

    async with client_for(search) as http:
        response = await http.post(f"{PREFIX}/search", json=body)

    assert response.status_code == 422
    assert search.queries == []


def test_the_503_is_documented_in_the_openapi_schema() -> None:
    app = FastAPI()
    add_search_routes(
        app.router, knowledge_search=lambda: FailingSearch(AssertionError())
    )

    responses = app.openapi()["paths"][f"{PREFIX}/search"]["post"]["responses"]

    assert "503" in responses
