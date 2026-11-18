"""Routes d'import sans base (revue S2 M-2) : ``add_document_routes`` reçoit
les fabriques de handlers ; ce test les substitue par les handlers réels
assemblés sur les doubles en mémoire (``fakes.make_harness``), sans
PostgreSQL ni volume de blobs. Couvre aussi la traduction HTTP du nom de
fichier refusé (M-3) : 422 ``INVALID_FILENAME`` au format ``{code, message}``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from knowledge_base.adapters.controller.rest.documents_router import (
    PREFIX,
    add_document_routes,
)
from tests.unit.knowledge_base.use_cases.fakes import Harness, make_harness

FORM = {"title": "Python avancé", "kind": "COURSE", "language": "fr"}


@pytest.fixture
def harness() -> Harness:
    return make_harness()


@pytest.fixture
async def client(harness: Harness) -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    add_document_routes(
        app.router,
        import_document_handler=lambda: harness.import_document,
        import_version_handler=lambda: harness.import_version,
        withdraw_document_handler=lambda: harness.withdraw_document,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


async def post_document(
    client: AsyncClient, content: bytes, filename: str = "cours.md"
) -> Response:
    return await client.post(
        f"{PREFIX}/documents",
        data=FORM,
        files={"file": (filename, content, "text/markdown")},
    )


async def test_the_substituted_handlers_serve_the_import(
    client: AsyncClient, harness: Harness
) -> None:
    response = await post_document(client, b"# Cours\n")

    assert response.status_code == 202
    assert list(harness.state.documents) == [UUID(response.json()["document_id"])]
    assert len(harness.state.items) == 1


async def test_the_substituted_version_handler_serves_the_version_import(
    client: AsyncClient, harness: Harness
) -> None:
    first = (await post_document(client, b"# V1\n")).json()

    response = await client.post(
        f"{PREFIX}/documents/{first['document_id']}/versions",
        files={"file": ("cours.md", b"# V2\n", "text/markdown")},
    )

    assert response.status_code == 202
    assert response.json()["number"] == 2


@pytest.mark.parametrize("filename", ["../../secret/cours.md", "..\\..\\cours.md"])
async def test_a_path_in_the_filename_is_dropped(
    client: AsyncClient, harness: Harness, filename: str
) -> None:
    response = await post_document(client, b"# Cours\n", filename)

    assert response.status_code == 202
    body = response.json()
    document = harness.state.documents[UUID(body["document_id"])]
    stored = document.get_version(UUID(body["version_id"])).source.filename
    assert stored == "cours.md"


@pytest.mark.parametrize("filename", ["x" * 253 + ".md", "dossier/", "..\\.."])
async def test_an_unusable_filename_is_a_422_invalid_filename(
    client: AsyncClient, harness: Harness, filename: str
) -> None:
    response = await post_document(client, b"# Cours\n", filename)

    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_FILENAME"
    assert harness.blobs.put_calls == 0
    assert harness.state.documents == {}
