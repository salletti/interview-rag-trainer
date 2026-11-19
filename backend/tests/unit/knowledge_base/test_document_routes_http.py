"""Routes d'import sans base (revue S2 M-2) : ``add_document_routes`` reçoit
les fabriques de handlers ; ce test les substitue par les handlers réels
assemblés sur les doubles en mémoire (``fakes.make_harness``), sans
PostgreSQL ni volume de blobs. Couvre aussi la traduction HTTP du nom de
fichier refusé (M-3) : 422 ``INVALID_FILENAME`` au format ``{code, message}``.

Refus anticipé sur ``Content-Length`` (revue S2, M-1) : un corps déclaré
au-delà de ``max_upload_bytes`` plus la marge du multipart est refusé en 413
``PAYLOAD_TOO_LARGE`` sans être lu ; un en-tête absent ou mensonger laisse le
contrôle au fil du flux (blob store).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Request, Response

from knowledge_base.adapters.controller.rest.documents_router import (
    MULTIPART_ALLOWANCE_BYTES,
    PREFIX,
    add_document_routes,
)
from tests.unit.knowledge_base.use_cases.fakes import (
    MAX_UPLOAD_BYTES,
    Harness,
    make_harness,
)

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
        max_upload_bytes=lambda: MAX_UPLOAD_BYTES,
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


# --- refus anticipé sur Content-Length (M-1) ----------------------------------------


class Body:
    """Corps envoyé en flux, qui note s'il a été lu par l'application."""

    def __init__(self, content: bytes) -> None:
        self.content = content
        self.read = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.read = True
        yield self.content


def multipart(content: bytes) -> tuple[bytes, str]:
    """Corps multipart d'un import et son ``Content-Type`` (frontière)."""
    request = Request(
        "POST",
        "http://test",
        data=FORM,
        files={"file": ("cours.md", content, "text/markdown")},
    )
    return request.read(), request.headers["Content-Type"]


async def post_stream(
    client: AsyncClient, path: str, body: Body, headers: dict[str, str]
) -> Response:
    return await client.post(path, content=body, headers=headers)


@pytest.mark.parametrize(
    "path",
    [f"{PREFIX}/documents", f"{PREFIX}/documents/{uuid4()}/versions"],
    ids=["document", "version"],
)
async def test_a_declared_length_over_the_limit_is_a_413_before_reading_the_body(
    client: AsyncClient, harness: Harness, path: str
) -> None:
    content, content_type = multipart(b"x" * 10)
    body = Body(content)
    declared = MAX_UPLOAD_BYTES + MULTIPART_ALLOWANCE_BYTES + 1

    response = await post_stream(
        client,
        path,
        body,
        {"Content-Type": content_type, "Content-Length": str(declared)},
    )

    assert response.status_code == 413
    assert response.json()["code"] == "PAYLOAD_TOO_LARGE"
    assert response.json()["max_bytes"] == MAX_UPLOAD_BYTES
    assert body.read is False
    assert harness.blobs.put_calls == 0
    assert harness.state.documents == {}


async def test_a_file_at_the_limit_is_not_refused_for_its_multipart_overhead(
    client: AsyncClient,
) -> None:
    """Le ``Content-Length`` compte frontières, en-têtes de parties et champs
    du formulaire : il dépasse la limite pour un fichier qui la respecte."""
    content, _ = multipart(b"x" * MAX_UPLOAD_BYTES)
    assert len(content) > MAX_UPLOAD_BYTES

    response = await post_document(client, b"x" * MAX_UPLOAD_BYTES)

    assert response.status_code == 202


@pytest.mark.parametrize(
    "declared", [None, "10"], ids=["absent-header", "lying-header"]
)
async def test_without_a_truthful_length_the_streaming_limit_still_refuses(
    client: AsyncClient, harness: Harness, declared: str | None
) -> None:
    content, content_type = multipart(b"x" * (MAX_UPLOAD_BYTES + 1))
    headers = {"Content-Type": content_type}
    if declared is not None:
        headers["Content-Length"] = declared

    response = await post_stream(client, f"{PREFIX}/documents", Body(content), headers)

    assert response.status_code == 413
    assert response.json()["code"] == "PAYLOAD_TOO_LARGE"
    assert harness.blobs.put_calls == 1
    assert harness.state.documents == {}
