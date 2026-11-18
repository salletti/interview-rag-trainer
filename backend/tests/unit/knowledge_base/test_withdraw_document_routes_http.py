"""Route de retrait sans base (S7 ; knowledge-base.md §5.1, §10, I12), sur les
handlers réels assemblés sur les doubles en mémoire (``s7_support``).

Contrat HTTP figé (préfixe ``/api/v1/knowledge-base`` de S2) :

- ``add_document_routes(router, *, import_document_handler,
  import_version_handler, withdraw_document_handler)`` : troisième fabrique, de
  ``WithdrawDocumentHandler`` (``documents_router``). Les tests existants
  d'``add_document_routes`` devront passer ce paramètre.
- ``DELETE /documents/{document_id}`` : retrait LOGIQUE (les lignes document et
  versions restent comme tombstones, §5.1) ; la purge est asynchrone.
  - 202 ``{"document_id", "status": "WITHDRAWN", "visibility_revision"}`` : le
    retrait est commité (effet immédiat, I12), la purge est planifiée ;
  - 404 ``{"code": "DOCUMENT_NOT_FOUND", "document_id"}`` ;
  - 409 ``{"code": "DOCUMENT_WITHDRAWN", "document_id"}`` si le document est déjà
    retiré : pas d'idempotence silencieuse (``KnowledgeDocument.withdraw``,
    test_knowledge_document_withdrawal) ; la révision n'est pas incrémentée
    deux fois et aucune seconde purge n'est planifiée ;
  - 409 ``{"code": "CONCURRENT_MODIFICATION"}`` quand le verrou optimiste est
    perdu deux fois de suite (même traduction que l'import) ;
  - 422 pour un identifiant qui n'est pas un UUID.
- Une fois retiré, ``POST /documents/{id}/versions`` répond 409
  ``DOCUMENT_WITHDRAWN`` (inchangé depuis S2).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from knowledge_base.adapters.controller.rest.documents_router import (
    PREFIX,
    add_document_routes,
)
from knowledge_base.domain.entities.knowledge_document import DocumentStatus
from tests.unit.knowledge_base.use_cases.indexing_fakes import qa_sheet
from tests.unit.knowledge_base.use_cases.s7_support import S7Rig, make_s7_rig


@pytest.fixture
def rig() -> S7Rig:
    return make_s7_rig()


@pytest.fixture
async def client(rig: S7Rig) -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    add_document_routes(
        app.router,
        import_document_handler=lambda: rig.base.harness.import_document,
        import_version_handler=rig.import_version_handler,
        withdraw_document_handler=rig.withdraw_handler,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


async def imported(rig: S7Rig) -> UUID:
    return (await rig.base.import_document(qa_sheet())).document_id


async def test_deleting_a_document_withdraws_it_and_answers_202(
    client: AsyncClient, rig: S7Rig
) -> None:
    document_id = await imported(rig)
    before = rig.base.document(document_id).visibility_revision

    response = await client.delete(f"{PREFIX}/documents/{document_id}")

    assert response.status_code == 202, response.text
    assert response.json() == {
        "document_id": str(document_id),
        "status": "WITHDRAWN",
        "visibility_revision": before + 1,
    }
    assert rig.base.document(document_id).status is DocumentStatus.WITHDRAWN


async def test_the_withdrawal_plans_exactly_one_purge(
    client: AsyncClient, rig: S7Rig
) -> None:
    document_id = await imported(rig)

    await client.delete(f"{PREFIX}/documents/{document_id}")

    assert rig.items_of_kind("purge_document") == [f"purge_document:{document_id}"]


async def test_an_unknown_document_is_a_404_document_not_found(
    client: AsyncClient, rig: S7Rig
) -> None:
    unknown = uuid4()

    response = await client.delete(f"{PREFIX}/documents/{unknown}")

    assert response.status_code == 404
    assert response.json()["code"] == "DOCUMENT_NOT_FOUND"
    assert response.json()["document_id"] == str(unknown)
    assert rig.items_of_kind("purge_document") == []


async def test_withdrawing_twice_is_a_409_and_changes_nothing_the_second_time(
    client: AsyncClient, rig: S7Rig
) -> None:
    document_id = await imported(rig)
    first = await client.delete(f"{PREFIX}/documents/{document_id}")
    revision = first.json()["visibility_revision"]

    second = await client.delete(f"{PREFIX}/documents/{document_id}")

    assert second.status_code == 409
    assert second.json()["code"] == "DOCUMENT_WITHDRAWN"
    assert second.json()["document_id"] == str(document_id)
    assert rig.base.document(document_id).visibility_revision == revision
    assert rig.items_of_kind("purge_document") == [f"purge_document:{document_id}"]


async def test_an_identifier_that_is_not_a_uuid_is_a_422(
    client: AsyncClient, rig: S7Rig
) -> None:
    await imported(rig)

    response = await client.delete(f"{PREFIX}/documents/pas-un-uuid")

    assert response.status_code == 422
    assert rig.items_of_kind("purge_document") == []


async def test_a_lost_optimistic_lock_is_a_409_concurrent_modification(
    client: AsyncClient, rig: S7Rig
) -> None:
    document_id = await imported(rig)
    rig.base.harness.state.conflict_on_save = True

    response = await client.delete(f"{PREFIX}/documents/{document_id}")

    assert response.status_code == 409
    assert response.json()["code"] == "CONCURRENT_MODIFICATION"
    assert rig.base.document(document_id).status is DocumentStatus.ACTIVE


async def test_a_withdrawn_document_refuses_new_versions_with_409(
    client: AsyncClient, rig: S7Rig
) -> None:
    document_id = await imported(rig)
    await client.delete(f"{PREFIX}/documents/{document_id}")

    response = await client.post(
        f"{PREFIX}/documents/{document_id}/versions",
        files={"file": ("fiche.md", qa_sheet(3, tag="b"), "text/markdown")},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "DOCUMENT_WITHDRAWN"
