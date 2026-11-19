"""Import REST des formats de S6 (roadmap S6 ; ADR 0012, point 4 : liste
blanche de types de média). Mêmes doubles en mémoire que
``test_document_routes_http`` ; la limite d'envoi est relevée (un DOCX pèse
quelques dizaines de Kio).

Contrat : ``application/pdf`` avec ``.pdf`` et le type OOXML de Word
(``DOCX``) avec ``.docx`` rejoignent la liste blanche (202) ; le type DOIT
concorder avec l'extension, comme pour Markdown/TXT ; le Word binaire
historique (``.doc``) reste refusé (415 ``UNSUPPORTED_MEDIA_TYPE``).

Les cas ``cours.pdf`` / ``application/pdf`` de
``test_import_document``, ``test_import_document_version`` et
``test_import_api`` (refus S2) deviennent faux avec S6 : l'implémentation de S6
doit les retirer de leurs listes de refus (voir le rapport).
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
from tests.office_support import DOCX, PDF, office_bytes
from tests.unit.knowledge_base.use_cases.fakes import Harness, make_harness

FORM = {"title": "Fiche pytest", "kind": "QA_SHEET", "language": "fr"}


@pytest.fixture
def harness() -> Harness:
    return make_harness(max_upload_bytes=1_000_000)


@pytest.fixture
async def client(harness: Harness) -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    add_document_routes(
        app.router,
        import_document_handler=lambda: harness.import_document,
        import_version_handler=lambda: harness.import_version,
        withdraw_document_handler=lambda: harness.withdraw_document,
        max_upload_bytes=lambda: 1_000_000,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


async def post_document(
    client: AsyncClient, content: bytes, filename: str, media_type: str
) -> Response:
    return await client.post(
        f"{PREFIX}/documents",
        data=FORM,
        files={"file": (filename, content, media_type)},
    )


@pytest.mark.parametrize(
    ("filename", "media_type"),
    [
        ("qa_pytest_fr.pdf", PDF),
        ("qa_pytest_fr.docx", DOCX),
        ("QA_PYTEST_FR.PDF", "Application/PDF"),
        ("qa_pytest_fr.docx", f"{DOCX}; charset=binary"),
    ],
    ids=["pdf", "docx", "case-insensitive", "media-type-parameter"],
)
async def test_a_pdf_or_docx_is_accepted_and_stored_with_its_bare_media_type(
    client: AsyncClient, harness: Harness, filename: str, media_type: str
) -> None:
    content = office_bytes(
        "qa_pytest_fr.pdf" if filename.lower().endswith(".pdf") else "qa_pytest_fr.docx"
    )

    response = await post_document(client, content, filename, media_type)

    assert response.status_code == 202, response.text
    body = response.json()
    version = harness.state.documents[UUID(body["document_id"])].get_version(
        UUID(body["version_id"])
    )
    assert version.source.media_type == media_type.split(";")[0].lower()
    assert version.source.size_bytes == len(content)
    assert len(harness.state.items) == 1


async def test_a_pdf_version_can_be_imported_on_an_existing_document(
    client: AsyncClient, harness: Harness
) -> None:
    first = await post_document(client, b"# Cours\n", "cours.md", "text/markdown")
    document_id = first.json()["document_id"]

    response = await client.post(
        f"{PREFIX}/documents/{document_id}/versions",
        files={"file": ("cours.pdf", office_bytes("cours_git_fr.pdf"), PDF)},
    )

    assert response.status_code == 202, response.text
    assert response.json()["number"] == 2


@pytest.mark.parametrize(
    ("filename", "media_type"),
    [
        ("cours.docx", PDF),
        ("cours.pdf", DOCX),
        ("cours.doc", "application/msword"),
        ("cours.docx", "application/msword"),
        ("cours.pdf", "text/plain"),
    ],
    ids=["docx-as-pdf", "pdf-as-docx", "legacy-doc", "docx-as-msword", "pdf-as-text"],
)
async def test_a_mismatched_or_legacy_office_type_stays_a_415(
    client: AsyncClient, harness: Harness, filename: str, media_type: str
) -> None:
    response = await post_document(client, b"PK\x03\x04", filename, media_type)

    assert response.status_code == 415
    assert response.json()["code"] == "UNSUPPORTED_MEDIA_TYPE"
    assert harness.blobs.put_calls == 0
    assert harness.state.documents == {}
