"""Visibilité (knowledge-base.md §10, §11 ; ADR 0010 §4, §5, §7) : après
publication et synchronisation, seuls les points de la version publiée sont
``visible=true`` ; PostgreSQL arbitre, Qdrant converge (PostgreSQL et Qdrant réels).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.indexing_support import (
    EMBEDDER_PARAMS,
    QA_SHEET,
    QA_SHEET_V2,
    IndexingRig,
    make_embedder,
    open_indexing_rig,
    payload,
)

pytestmark = pytest.mark.integration


@pytest.fixture(params=EMBEDDER_PARAMS)
async def rig(
    request: pytest.FixtureRequest,
    integration_settings: Settings,
    session_factory: SessionFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[IndexingRig]:
    embedder = make_embedder(request.param)
    async with open_indexing_rig(
        integration_settings, session_factory, tmp_path, monkeypatch, embedder
    ) as opened:
        yield opened


async def test_between_publication_and_synchronization_the_new_points_are_invisible(
    rig: IndexingRig,
) -> None:
    index_id = await rig.provision()
    first = await rig.import_and_extract()
    await rig.run_index(first.version_id, index_id)
    await rig.run_sync(first.document_id, 1)
    second = await rig.import_version_and_extract(first.document_id, QA_SHEET_V2)

    await rig.run_index(second.version_id, index_id)

    document = await rig.document(first.document_id)
    assert document["published_version_id"] == second.version_id
    by_version: dict[str, set[bool]] = {}
    for record in await rig.points(index_id):
        body = payload(record)
        by_version.setdefault(body["version_id"], set()).add(body["visible"])
    # L'ancienne version reste visible dans Qdrant (le contrôle SQL l'écarte),
    # la nouvelle ne l'est pas encore : le document ne peut pas apparaître à tort.
    assert by_version == {
        str(first.version_id): {True},
        str(second.version_id): {False},
    }


async def test_after_the_synchronization_only_the_published_version_is_visible(
    rig: IndexingRig,
) -> None:
    index_id = await rig.provision()
    first = await rig.import_and_extract()
    await rig.run_index(first.version_id, index_id)
    await rig.run_sync(first.document_id, 1)
    second = await rig.import_version_and_extract(first.document_id, QA_SHEET_V2)
    await rig.run_index(second.version_id, index_id)

    assert await rig.run_sync(first.document_id, 2) == "succeeded"

    records = await rig.points(index_id)
    visible = [payload(r) for r in records if payload(r)["visible"]]
    assert len(records) == len(visible) == 2
    assert {p["version_id"] for p in visible} == {str(second.version_id)}


async def test_the_synchronization_leaves_the_other_documents_alone(
    rig: IndexingRig,
) -> None:
    index_id = await rig.provision()
    mine = await rig.import_and_extract()
    other = await rig.import_and_extract(
        QA_SHEET.replace(b"FastAPI", b"Pydantic"), kind="QA_SHEET"
    )
    await rig.run_index(mine.version_id, index_id)
    await rig.run_index(other.version_id, index_id)

    await rig.run_sync(mine.document_id, 1)

    by_document: dict[str, set[bool]] = {}
    for record in await rig.points(index_id):
        body = payload(record)
        by_document.setdefault(body["document_id"], set()).add(body["visible"])
    assert by_document == {
        str(mine.document_id): {True},
        str(other.document_id): {False},
    }
