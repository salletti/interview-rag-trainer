"""Routes de consultation et de rejeu des échecs, sans base (S8 ; roadmap S8
« endpoint de lecture des échecs » et ``RetryIngestion`` ; ADR 0004 points 10
et 11 : l'état ``FAILED`` en base joue le rôle de dead-letter, « consultable et
relançable »).

Le routeur traduit HTTP en commande et le résultat (ou le refus) en réponse ; il
ne décide rien. Les handlers sont des doubles : la frontière testée est le
contrat HTTP.

API figée :

- ``knowledge_base.adapters.controller.rest.failures_router.
  add_failure_routes(router, *, list_failures_handler, retry_ingestion_handler)``
  (fabriques appelées à chaque requête, comme ``add_document_routes`` ; cette
  fonction est NOUVELLE : ``add_document_routes`` ne change pas de signature).
- ``GET /api/v1/knowledge-base/failures?limit=`` (``limit`` 1 à 500, défaut
  100 ; 422 sinon) -> 200 ``{"items": [...]}`` ; chaque élément : ``item_id``,
  ``kind``, ``code``, ``message``, ``attempts``, ``max_attempts``,
  ``failed_at`` (ISO 8601), ``replayable`` (booléen, d'après le catalogue),
  ``document_id``, ``version_id``, ``index_id`` (``null`` s'ils n'existent pas
  dans le payload de l'item).
- ``POST /api/v1/knowledge-base/failures/{item_id}/retry`` -> 202
  ``{"item_id", "kind", "status": "PENDING"}`` ; 404 ``FAILURE_NOT_FOUND`` ;
  409 ``NOT_FAILED`` (avec ``status``) ; 409 ``NOT_REPLAYABLE`` (avec
  ``failure_code``, le code de l'échec) ; 409 ``OBSOLETE_WORK`` (avec
  ``reason`` : version supplantée ou document retiré) ; 422 pour un
  identifiant invalide. Tout refus porte ``code``, ``message`` et ``item_id``.
- Cas d'usage : ``use_cases.list_failures`` (``ListFailures(limit)``,
  ``ListFailuresHandler.handle -> tuple[FailureView, ...]``,
  ``FailureView`` dans ``failure_view.py``) et ``use_cases.retry_ingestion``
  (``RetryIngestion(item_id)``, ``RetryIngestionHandler.handle ->
  RetryIngestionResult(item_id, kind)``, refus dans ``errors.py`` :
  ``FailureNotFoundError(item_id)``, ``ItemNotFailedError(item_id, status)``,
  ``NotReplayableError(item_id, code)``, ``ObsoleteWorkError(item_id, reason)``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from knowledge_base.adapters.controller.rest.documents_router import PREFIX
from knowledge_base.adapters.controller.rest.failures_router import add_failure_routes
from knowledge_base.use_cases.list_failures.failure_view import FailureView
from knowledge_base.use_cases.list_failures.list_failures_command import ListFailures
from knowledge_base.use_cases.list_failures.list_failures_handler import (
    ListFailuresHandler,
)
from knowledge_base.use_cases.retry_ingestion.errors import (
    FailureNotFoundError,
    ItemNotFailedError,
    NotReplayableError,
    ObsoleteWorkError,
)
from knowledge_base.use_cases.retry_ingestion.retry_ingestion_command import (
    RetryIngestion,
)
from knowledge_base.use_cases.retry_ingestion.retry_ingestion_handler import (
    RetryIngestionHandler,
)
from knowledge_base.use_cases.retry_ingestion.retry_ingestion_result import (
    RetryIngestionResult,
)

ITEM_ID = UUID(int=7)
FAILED_AT = datetime(2026, 11, 19, 8, 30, tzinfo=UTC)


def view(**overrides: object) -> FailureView:
    fields: dict[str, object] = {
        "item_id": ITEM_ID,
        "kind": "extract_version",
        "code": "BLOB_MISSING",
        "message": "original absent du volume",
        "attempts": 1,
        "max_attempts": 5,
        "failed_at": FAILED_AT,
        "replayable": True,
        "document_id": UUID(int=1),
        "version_id": UUID(int=2),
        "index_id": None,
    }
    fields.update(overrides)
    return FailureView(**fields)  # type: ignore[arg-type]


class StubList:
    def __init__(self, *views: FailureView) -> None:
        self.views = views
        self.queries: list[ListFailures] = []

    async def handle(self, query: ListFailures) -> tuple[FailureView, ...]:
        self.queries.append(query)
        return self.views


class StubRetry:
    def __init__(self, *, refusal: Exception | None = None) -> None:
        self.refusal = refusal
        self.commands: list[RetryIngestion] = []

    async def handle(self, command: RetryIngestion) -> RetryIngestionResult:
        self.commands.append(command)
        if self.refusal is not None:
            raise self.refusal
        return RetryIngestionResult(item_id=command.item_id, kind="extract_version")


def client_for(
    listing: StubList | None = None, retry: StubRetry | None = None
) -> AsyncClient:
    app = FastAPI()
    stub_list, stub_retry = listing or StubList(), retry or StubRetry()
    add_failure_routes(
        app.router,
        list_failures_handler=lambda: cast(ListFailuresHandler, stub_list),
        retry_ingestion_handler=lambda: cast(RetryIngestionHandler, stub_retry),
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def http() -> AsyncIterator[AsyncClient]:
    async with client_for() as client:
        yield client


# --- GET /failures -------------------------------------------------------------------


async def test_listing_failures_returns_every_field_of_a_failed_item() -> None:
    async with client_for(StubList(view())) as http:
        response = await http.get(f"{PREFIX}/failures")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "items": [
            {
                "item_id": str(ITEM_ID),
                "kind": "extract_version",
                "code": "BLOB_MISSING",
                "message": "original absent du volume",
                "attempts": 1,
                "max_attempts": 5,
                "failed_at": "2026-11-19T08:30:00Z",
                "replayable": True,
                "document_id": str(UUID(int=1)),
                "version_id": str(UUID(int=2)),
                "index_id": None,
            }
        ]
    }


async def test_listing_without_failure_is_an_empty_list_not_an_error(
    http: AsyncClient,
) -> None:
    response = await http.get(f"{PREFIX}/failures")

    assert response.status_code == 200
    assert response.json() == {"items": []}


async def test_a_non_replayable_failure_is_reported_as_such() -> None:
    stub = StubList(view(code="EMPTY_DOCUMENT", replayable=False))
    async with client_for(stub) as http:
        (item,) = (await http.get(f"{PREFIX}/failures")).json()["items"]

    assert (item["code"], item["replayable"]) == ("EMPTY_DOCUMENT", False)


async def test_the_limit_is_forwarded_and_defaults_to_100() -> None:
    stub = StubList()
    async with client_for(stub) as http:
        await http.get(f"{PREFIX}/failures")
        await http.get(f"{PREFIX}/failures", params={"limit": 5})

    assert [q.limit for q in stub.queries] == [100, 5]


@pytest.mark.parametrize("limit", ["0", "-1", "501", "x"])
async def test_an_invalid_limit_is_a_422_and_reads_nothing(limit: str) -> None:
    stub = StubList()
    async with client_for(stub) as http:
        response = await http.get(f"{PREFIX}/failures", params={"limit": limit})

    assert response.status_code == 422
    assert stub.queries == []


# --- POST /failures/{id}/retry -------------------------------------------------------


async def test_retrying_a_failure_answers_202_with_the_reopened_item() -> None:
    stub = StubRetry()
    async with client_for(retry=stub) as http:
        response = await http.post(f"{PREFIX}/failures/{ITEM_ID}/retry")

    assert response.status_code == 202, response.text
    assert response.json() == {
        "item_id": str(ITEM_ID),
        "kind": "extract_version",
        "status": "PENDING",
    }
    assert stub.commands == [RetryIngestion(item_id=ITEM_ID)]


@pytest.mark.parametrize(
    ("refusal", "status", "body"),
    [
        (
            FailureNotFoundError(ITEM_ID),
            404,
            {"code": "FAILURE_NOT_FOUND"},
        ),
        (
            ItemNotFailedError(ITEM_ID, "PENDING"),
            409,
            {"code": "NOT_FAILED", "status": "PENDING"},
        ),
        (
            NotReplayableError(ITEM_ID, "EMPTY_DOCUMENT"),
            409,
            {"code": "NOT_REPLAYABLE", "failure_code": "EMPTY_DOCUMENT"},
        ),
        (
            ObsoleteWorkError(ITEM_ID, "version supplantée"),
            409,
            {"code": "OBSOLETE_WORK", "reason": "version supplantée"},
        ),
    ],
    ids=["unknown", "not-failed", "not-replayable", "obsolete"],
)
async def test_each_refusal_has_its_status_and_a_stable_code(
    refusal: Exception, status: int, body: dict[str, object]
) -> None:
    async with client_for(retry=StubRetry(refusal=refusal)) as http:
        response = await http.post(f"{PREFIX}/failures/{ITEM_ID}/retry")

    assert response.status_code == status
    payload = response.json()
    assert {key: payload[key] for key in body} == body
    assert payload["message"]
    assert payload["item_id"] == str(ITEM_ID)


async def test_an_invalid_item_id_is_a_422_and_retries_nothing() -> None:
    stub = StubRetry()
    async with client_for(retry=stub) as http:
        response = await http.post(f"{PREFIX}/failures/pas-un-uuid/retry")

    assert response.status_code == 422
    assert stub.commands == []


async def test_the_retry_route_is_a_post_and_the_list_route_a_get(
    http: AsyncClient,
) -> None:
    assert (await http.get(f"{PREFIX}/failures/{uuid4()}/retry")).status_code == 405
    assert (await http.post(f"{PREFIX}/failures")).status_code == 405
