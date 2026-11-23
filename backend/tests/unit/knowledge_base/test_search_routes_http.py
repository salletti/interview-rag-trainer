"""Route de recherche sans base ni Qdrant (S5 ; knowledge-base.md §14).

``add_search_routes(router, *, knowledge_search)`` (module
``knowledge_base.adapters.controller.rest.search_router``) reçoit une FABRIQUE
de ``KnowledgeSearch`` (le contrat public) : l'API REST sert la recherche par la
même porte que les autres contextes. Ce test la substitue par un double qui
enregistre le ``KnowledgeQuery`` reçu.

Contrat HTTP figé (préfixe ``/api/v1/knowledge-base`` de S2) :

- ``POST /search``, corps JSON ``{"text": str, "limit": int = 5, "language":
  str | null, "topics": [str], "kinds": [str], "document_ids": [uuid]}`` ;
- 200, ``{"index_id", "complete", "passages": [{"unit_id", "unit_type",
  "question", "text", "truncated", "matched_chunk_ids", "score", "citation":
  {"document_id", "document_title", "version_id", "version_number",
  "section_path", "page_start", "page_end", "source_filename"}}]}`` : les noms
  du contrat §14, UUID en texte ;
- 422 (corps invalide : texte vide ou blanc, ``limit < 1``, UUID illisible).

Non figé : le plafond de ``limit``, le code HTTP quand l'index actif manque.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from knowledge_base.adapters.controller.rest.documents_router import PREFIX
from knowledge_base.adapters.controller.rest.search_router import add_search_routes
from knowledge_base.contracts.result import (
    Citation,
    KnowledgeQuery,
    KnowledgeSearchResult,
    RetrievedPassage,
)
from runtime.app import create_app
from tests.support import unreachable_settings

INDEX = UUID(int=1)
UNIT = UUID(int=2)
CHUNK = UUID(int=3)
DOCUMENT = UUID(int=4)
VERSION = UUID(int=5)


TEXT = "Question : Qu'est-ce que Depends ?\n\nUne dépendance injectée."


def passage(**overrides: object) -> RetrievedPassage:
    fields: dict[str, object] = {
        "unit_id": UNIT,
        "unit_type": "QA",
        "question": "Qu'est-ce que Depends ?",
        "text": TEXT,
        "truncated": False,
        "matched_chunk_ids": (CHUNK,),
        "score": 0.83,
        "citation": Citation(
            document_id=DOCUMENT,
            document_title="Fiche FastAPI",
            version_id=VERSION,
            version_number=2,
            section_path=("Fiche FastAPI", "Injection"),
            page_start=3,
            page_end=4,
            source_filename="fastapi.md",
        ),
    }
    fields.update(overrides)
    return RetrievedPassage(**fields)  # type: ignore[arg-type]


class RecordingSearch:
    def __init__(self, result: KnowledgeSearchResult) -> None:
        self.result = result
        self.queries: list[KnowledgeQuery] = []

    async def search(self, query: KnowledgeQuery) -> KnowledgeSearchResult:
        self.queries.append(query)
        return self.result


@pytest.fixture
def search() -> RecordingSearch:
    return RecordingSearch(
        KnowledgeSearchResult(passages=(passage(),), complete=True, index_id=INDEX)
    )


@pytest.fixture
async def client(search: RecordingSearch) -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    add_search_routes(app.router, knowledge_search=lambda: search)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


async def test_a_search_returns_the_passages_with_their_citation(
    client: AsyncClient,
) -> None:
    response = await client.post(f"{PREFIX}/search", json={"text": "Depends"})

    assert response.status_code == 200
    assert response.json() == {
        "index_id": str(INDEX),
        "complete": True,
        "passages": [
            {
                "unit_id": str(UNIT),
                "unit_type": "QA",
                "question": "Qu'est-ce que Depends ?",
                "text": TEXT,
                "truncated": False,
                "matched_chunk_ids": [str(CHUNK)],
                "score": 0.83,
                "citation": {
                    "document_id": str(DOCUMENT),
                    "document_title": "Fiche FastAPI",
                    "version_id": str(VERSION),
                    "version_number": 2,
                    "section_path": ["Fiche FastAPI", "Injection"],
                    "page_start": 3,
                    "page_end": 4,
                    "source_filename": "fastapi.md",
                },
            }
        ],
    }


async def test_the_body_becomes_a_knowledge_query(
    client: AsyncClient, search: RecordingSearch
) -> None:
    other = UUID(int=77)

    await client.post(
        f"{PREFIX}/search",
        json={
            "text": "Depends",
            "limit": 3,
            "language": "fr",
            "topics": ["python", "fastapi"],
            "kinds": ["QA_SHEET"],
            "document_ids": [str(other)],
        },
    )

    assert search.queries == [
        KnowledgeQuery(
            text="Depends",
            limit=3,
            language="fr",
            topics=("python", "fastapi"),
            kinds=("QA_SHEET",),
            document_ids=(other,),
        )
    ]


async def test_only_the_text_is_required_and_the_default_limit_is_five(
    client: AsyncClient, search: RecordingSearch
) -> None:
    await client.post(f"{PREFIX}/search", json={"text": "Depends"})

    assert search.queries == [KnowledgeQuery(text="Depends")]
    assert search.queries[0].limit == 5


async def test_an_incomplete_result_and_absent_values_are_serialized_as_such(
    client: AsyncClient, search: RecordingSearch
) -> None:
    search.result = KnowledgeSearchResult(
        passages=(
            passage(
                question=None,
                unit_type="SECTION",
                truncated=True,
                citation=Citation(
                    document_id=DOCUMENT,
                    document_title="Cours",
                    version_id=VERSION,
                    version_number=1,
                    section_path=(),
                    page_start=None,
                    page_end=None,
                    source_filename="cours.md",
                ),
            ),
        ),
        complete=False,
        index_id=INDEX,
    )

    body = (await client.post(f"{PREFIX}/search", json={"text": "asyncio"})).json()

    assert body["complete"] is False
    (found,) = body["passages"]
    assert found["question"] is None
    assert found["truncated"] is True
    assert found["citation"]["page_start"] is None
    assert found["citation"]["page_end"] is None
    assert found["citation"]["section_path"] == []


async def test_a_result_without_passage_is_an_empty_list_not_an_error(
    client: AsyncClient, search: RecordingSearch
) -> None:
    search.result = KnowledgeSearchResult(passages=(), complete=False, index_id=INDEX)

    response = await client.post(f"{PREFIX}/search", json={"text": "rien"})

    assert response.status_code == 200
    assert response.json()["passages"] == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"text": ""},
        {"text": "   "},
        {"text": "x", "limit": 0},
        {"text": "x", "limit": -3},
        {"text": "x", "limit": "beaucoup"},
        {"text": "x", "document_ids": ["pas-un-uuid"]},
        {"text": "x", "topics": "python"},
    ],
)
async def test_an_invalid_body_is_refused_before_any_search(
    client: AsyncClient, search: RecordingSearch, body: dict[str, object]
) -> None:
    response = await client.post(f"{PREFIX}/search", json=body)

    assert response.status_code == 422
    assert search.queries == []


async def test_the_search_route_only_accepts_post(client: AsyncClient) -> None:
    response = await client.get(f"{PREFIX}/search", params={"text": "Depends"})

    assert response.status_code == 405


# --- montage par ``knowledge_base.register`` ----------------------------------


def test_create_app_mounts_the_search_route() -> None:
    app = create_app(unreachable_settings())

    routes = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in (route.methods or set())
    }

    assert ("POST", f"{PREFIX}/search") in routes
    # Les routes d'import de S2 restent montées.
    assert ("POST", f"{PREFIX}/documents") in routes


def test_the_search_route_is_documented_in_the_openapi_schema() -> None:
    schema = create_app(unreachable_settings()).openapi()

    operation = schema["paths"][f"{PREFIX}/search"]["post"]
    assert "200" in operation["responses"]
    assert "422" in operation["responses"]
