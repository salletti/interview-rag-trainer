"""Retrait et purge (S7 ; knowledge-base.md §4.1 I12, §5.1, §7.3 condition 6,
§10 ; ADR 0010 point 8, ADR 0012 point 6), sur PostgreSQL, Qdrant, volume de
blobs et work queue réels.

Contrat vérifié :

- ``DELETE /documents/{id}`` : 202, commit immédiat. Toute recherche commencée
  APRÈS ce commit ignore le document (contrôle SQL final), alors que Qdrant porte
  encore ses points visibles ; une recherche commencée avant peut encore le
  rendre (§10).
- ``purge_document`` : 0 point du document dans l'index, ses unités et chunks
  supprimés, ``documents.purged_at`` renseigné, blobs de ses versions supprimés
  (ceux des autres documents conservés) ; les lignes document et versions restent
  (tombstones). Rejouable.
- Une tentative zombie d'``index_version`` qui écrit APRÈS la purge laisse des
  points ``visible=false`` (jamais renvoyés) ; une nouvelle exécution de la
  purge (la réconciliation de S8 la planifiera) les supprime. Rien en S7 ne
  la déclenche seule.
- Le retrait annule le travail en cours : une extraction qui finalise après un
  retrait est périmée et n'écrit aucun chunk.
- Import de version et retrait concurrents, dans les deux ordres possibles :
  le retrait aboutit toujours, aucun travail d'extraction ou d'indexation ne
  reste ``PENDING`` ou ``RUNNING``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import pytest

from knowledge_base.adapters.gateway.blob.filesystem_blob_store import (
    FileSystemBlobStore,
)
from knowledge_base.domain.exceptions.document_errors import DocumentWithdrawnError
from knowledge_base.use_cases.import_document_version.import_document_version_command import (  # noqa: E501
    ImportDocumentVersion,
)
from knowledge_base.use_cases.import_document_version.import_document_version_handler import (  # noqa: E501
    ImportDocumentVersionHandler,
)
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.import_support import blob_path
from tests.integration.knowledge_base.indexing_support import (
    QA_SHEET,
    QA_SHEET_V2,
    payload,
)
from tests.integration.knowledge_base.s7_support import (
    PYDANTIC_SHEET,
    TIMEOUT,
    Background,
    CoordinatedUnitOfWork,
    Coordination,
    GatedVectorIndex,
    InterferingHandler,
    S7Rig,
    UnreachableVectorIndex,
    background_tasks,
    documents_of,
    open_s7_rig,
    query,
)
from tests.integration.knowledge_base.support import enqueue_committed

pytestmark = pytest.mark.integration


@pytest.fixture
async def background(rig: S7Rig) -> AsyncIterator[Background]:
    """Détruit avant ``rig`` : aucune tâche ne survit au corpus."""
    async with background_tasks() as tasks:
        yield tasks


@pytest.fixture
async def rig(
    integration_settings: Settings,
    session_factory: SessionFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[S7Rig]:
    async with open_s7_rig(
        integration_settings, session_factory, tmp_path, monkeypatch
    ) as opened:
        yield opened


# --- le retrait est immédiat ---------------------------------------------------------


async def test_the_withdrawal_answers_202_and_marks_the_document_withdrawn(
    rig: S7Rig,
) -> None:
    published = await rig.publish()

    response = await rig.withdraw(published.document_id)

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["document_id"] == str(published.document_id)
    assert body["status"] == "WITHDRAWN"
    document = await rig.indexing.document(published.document_id)
    assert document["status"] == "WITHDRAWN"
    assert document["visibility_revision"] == body["visibility_revision"] == 2
    purges = [
        i
        for i in await rig.items(published.document_id)
        if i["kind"] == "purge_document"
    ]
    assert [p["status"] for p in purges] == ["PENDING"]


async def test_a_withdrawn_document_is_not_found_even_before_the_purge(
    rig: S7Rig,
) -> None:
    mine = await rig.publish(title="Retirée")
    other = await rig.publish(PYDANTIC_SHEET, title="Témoin")
    assert documents_of(await rig.search.search(query())) == {
        mine.document_id,
        other.document_id,
    }

    await rig.withdraw(mine.document_id)

    # Qdrant porte toujours les points visibles : c'est le contrôle SQL qui
    # écarte le document, pas la purge.
    assert any(payload(p)["visible"] for p in await rig.points_of(mine.document_id))
    assert documents_of(await rig.search.search(query())) == {other.document_id}


async def test_no_search_started_after_the_withdrawal_commit_returns_the_document(
    rig: S7Rig, background: Background
) -> None:
    mine = await rig.publish(title="Retirée")
    other = await rig.publish(PYDANTIC_SHEET, title="Témoin")
    searchers = 3
    committed = asyncio.Event()
    enough = asyncio.Event()
    warmed = asyncio.Barrier(searchers + 1)
    stop = asyncio.Event()
    started: list[tuple[bool, set[UUID]]] = []

    async def searcher() -> None:
        first = True
        while not stop.is_set():
            after_commit = committed.is_set()  # lu AVANT de lancer la recherche
            found = documents_of(await rig.search.search(query()))
            started.append((after_commit, found))
            if first:
                first = False
                async with asyncio.timeout(TIMEOUT):
                    await warmed.wait()
            if sum(1 for after, _ in started if after) >= 3 * searchers:
                enough.set()

    tasks = [background.spawn(searcher()) for _ in range(searchers)]
    async with asyncio.timeout(TIMEOUT):
        await warmed.wait()  # chaque chercheur a déjà trouvé le document
    response = await rig.withdraw(mine.document_id)
    committed.set()
    async with asyncio.timeout(TIMEOUT):
        await enough.wait()
    stop.set()
    await asyncio.gather(*tasks)

    assert response.status_code == 202
    before = [found for after, found in started if not after]
    assert before and all(mine.document_id in found for found in before[:searchers])
    after = [found for after_commit, found in started if after_commit]
    assert after, "aucune recherche n'a commencé après le commit"
    assert all(mine.document_id not in found for found in after), after
    assert all(other.document_id in found for found in after)


# --- la purge ------------------------------------------------------------------------


async def test_the_purge_deletes_every_point_of_the_document_and_only_its_own(
    rig: S7Rig,
) -> None:
    mine = await rig.publish(title="Retirée")
    other = await rig.publish(PYDANTIC_SHEET, title="Témoin")
    second = await rig.post_version(mine.document_id, QA_SHEET_V2)
    await rig.extract(second)
    assert await rig.indexing.run_index(second.version_id, rig.index_id) == "succeeded"
    kept = {p.id for p in await rig.points_of(other.document_id)}
    versions = {payload(p)["version_id"] for p in await rig.points_of(mine.document_id)}
    assert len(versions) == 2  # points des versions 1 et 2 avant la purge
    await rig.withdraw(mine.document_id)

    assert await rig.run_purge(mine.document_id) == "succeeded"

    assert await rig.points_of(mine.document_id) == []
    assert {p.id for p in await rig.points_of(other.document_id)} == kept
    assert documents_of(await rig.search.search(query())) == {other.document_id}


async def test_the_purge_deletes_units_and_chunks_and_records_the_purge_date(
    rig: S7Rig,
) -> None:
    mine = await rig.publish(title="Retirée")
    other = await rig.publish(PYDANTIC_SHEET, title="Témoin")
    chunks_of_other = await rig.count_chunks(other.document_id)
    units_of_other = await rig.count_units(other.document_id)
    assert await rig.count_chunks(mine.document_id) > 0
    assert (
        await rig.row(
            "SELECT purged_at FROM knowledge_base.documents WHERE id = :d",
            d=mine.document_id,
        )
    )["purged_at"] is None
    await rig.withdraw(mine.document_id)

    await rig.run_purge(mine.document_id)

    assert await rig.count_chunks(mine.document_id) == 0
    assert await rig.count_units(mine.document_id) == 0
    assert await rig.count_chunks(other.document_id) == chunks_of_other
    assert await rig.count_units(other.document_id) == units_of_other
    purged = await rig.row(
        "SELECT purged_at, status FROM knowledge_base.documents WHERE id = :d",
        d=mine.document_id,
    )
    assert purged["purged_at"] is not None
    assert purged["status"] == "WITHDRAWN"
    untouched = await rig.row(
        "SELECT purged_at FROM knowledge_base.documents WHERE id = :d",
        d=other.document_id,
    )
    assert untouched["purged_at"] is None


async def test_the_tombstone_rows_survive_the_purge(rig: S7Rig) -> None:
    mine = await rig.publish(title="Retirée")
    second = await rig.post_version(mine.document_id, QA_SHEET_V2)
    await rig.withdraw(mine.document_id)
    imports = rig.indexing.extraction.imports
    statuses = [v["status"] for v in await imports.versions(mine.document_id)]

    await rig.run_purge(mine.document_id)

    versions = await imports.versions(mine.document_id)
    assert [v["status"] for v in versions] == statuses == ["PUBLISHED", "RECEIVED"]
    assert versions[1]["id"] == second.version_id


async def test_the_purge_deletes_the_blobs_of_the_document_and_keeps_the_others(
    rig: S7Rig,
) -> None:
    mine = await rig.publish(QA_SHEET, title="Retirée")
    await rig.post_version(mine.document_id, QA_SHEET_V2)
    await rig.publish(PYDANTIC_SHEET, title="Témoin")
    assert blob_path(rig.blob_root, QA_SHEET).is_file()
    assert blob_path(rig.blob_root, QA_SHEET_V2).is_file()
    await rig.withdraw(mine.document_id)

    await rig.run_purge(mine.document_id)

    assert not blob_path(rig.blob_root, QA_SHEET).exists()
    assert not blob_path(rig.blob_root, QA_SHEET_V2).exists()
    assert blob_path(rig.blob_root, PYDANTIC_SHEET).is_file()
    assert rig.blob_root.exists()


async def test_the_content_of_a_purged_document_cannot_be_reimported(
    rig: S7Rig,
) -> None:
    mine = await rig.publish(QA_SHEET, title="Retirée")
    await rig.withdraw(mine.document_id)
    await rig.run_purge(mine.document_id)

    response = await rig.indexing.extraction.imports.post_document(
        QA_SHEET, kind="QA_SHEET", title="Retour", filename="fiche.md"
    )

    # Le tombstone garde l'empreinte (O2, O11) : le fichier n'existe plus, la
    # ligne de version le « possède » toujours.
    assert response.status_code == 409
    assert response.json()["code"] == "DOCUMENT_WITHDRAWN"


async def test_a_second_purge_of_the_same_document_changes_nothing(rig: S7Rig) -> None:
    mine = await rig.publish(QA_SHEET, title="Retirée")
    other = await rig.publish(PYDANTIC_SHEET, title="Témoin")
    await rig.withdraw(mine.document_id)
    await rig.run_purge(mine.document_id)
    kept = {p.id for p in await rig.points_of(other.document_id)}
    replay = await enqueue_committed(
        rig.session_factory,
        "purge_document",
        f"purge_document:{mine.document_id}:replay",
        payload={"document_id": str(mine.document_id)},
    )

    outcome = await rig.indexing.extraction.run(replay, rig.purge_handler())

    assert outcome == "succeeded"
    assert await rig.points_of(mine.document_id) == []
    assert {p.id for p in await rig.points_of(other.document_id)} == kept
    assert blob_path(rig.blob_root, PYDANTIC_SHEET).is_file()


async def test_a_purge_that_cannot_reach_qdrant_is_retried_and_deletes_nothing_else(
    rig: S7Rig,
) -> None:
    mine = await rig.publish(QA_SHEET, title="Retirée")
    await rig.withdraw(mine.document_id)

    handler = rig.purge_handler(UnreachableVectorIndex(rig.indexing.vector))

    outcome = await rig.run_purge(mine.document_id, handler)

    assert outcome == "retry_scheduled"
    assert blob_path(rig.blob_root, QA_SHEET).is_file()
    assert await rig.count_chunks(mine.document_id) > 0
    assert await rig.points_of(mine.document_id)
    document = await rig.row(
        "SELECT purged_at FROM knowledge_base.documents WHERE id = :d",
        d=mine.document_id,
    )
    assert document["purged_at"] is None


# --- zombie après la purge -----------------------------------------------------------


async def test_a_zombie_indexing_writing_after_the_purge_leaves_only_invisible_points(
    rig: S7Rig, background: Background
) -> None:
    mine = await rig.publish(QA_SHEET, title="Retirée")
    second = await rig.post_version(mine.document_id, QA_SHEET_V2)
    await rig.extract(second)
    gate = GatedVectorIndex(rig.indexing.vector, gate_on="upsert")
    zombie = background.spawn(
        rig.indexing.run_index(
            second.version_id, rig.index_id, rig.indexing.index_handler_over(gate)
        )
    )
    async with asyncio.timeout(TIMEOUT):
        await gate.reached.wait()  # N+1 est en train d'indexer, pas encore écrite

    await rig.withdraw(mine.document_id)
    assert await rig.run_purge(mine.document_id) == "succeeded"
    assert await rig.points_of(mine.document_id) == []
    gate.release.set()  # la tentative zombie écrit APRÈS la purge
    outcome = await asyncio.wait_for(zombie, TIMEOUT)

    assert outcome == "stale"
    late = await rig.points_of(mine.document_id)
    assert late, "la tentative zombie a réécrit des points après la purge"
    assert {payload(p)["version_id"] for p in late} == {str(second.version_id)}
    assert not any(payload(p)["visible"] for p in late)
    assert (await rig.search.search(query())).passages == ()
    document = await rig.indexing.document(mine.document_id)
    assert document["published_version_id"] == mine.version_id  # N+1 jamais publiée

    # Une nouvelle exécution de la purge (planifiée par la réconciliation, S8)
    # les supprime.
    replay = await enqueue_committed(
        rig.session_factory,
        "purge_document",
        f"purge_document:{mine.document_id}:reconciliation",
        payload={"document_id": str(mine.document_id)},
    )
    assert await rig.indexing.extraction.run(replay, rig.purge_handler()) == "succeeded"
    assert await rig.points_of(mine.document_id) == []


# --- retrait pendant le travail ------------------------------------------------------


async def test_an_extraction_finishing_after_the_withdrawal_writes_no_chunk(
    rig: S7Rig,
) -> None:
    imported = await rig.indexing.extraction.import_document(QA_SHEET)

    async def withdraw_meanwhile() -> None:
        assert (await rig.withdraw(imported.document_id)).status_code == 202

    handler = InterferingHandler(rig.indexing.extraction.handler, withdraw_meanwhile)

    outcome = await rig.indexing.extraction.run(
        imported.item_id, handler.as_work_handler()
    )

    assert outcome == "stale"
    assert await rig.count_chunks(imported.document_id) == 0
    assert await rig.count_units(imported.document_id) == 0
    version = await rig.indexing.extraction.version(imported.version_id)
    assert version["status"] == "RECEIVED"
    items = {i["kind"]: i["status"] for i in await rig.items(imported.document_id)}
    assert items["extract_version"] == "CANCELLED"
    assert "index_version" not in items


async def test_the_withdrawal_cancels_an_indexation_in_progress(rig: S7Rig) -> None:
    pending = await rig.import_extract()

    await rig.withdraw(pending.document_id)

    items = {i["kind"]: i["status"] for i in await rig.items(pending.document_id)}
    assert items["index_version"] == "CANCELLED"
    indexation = await rig.indexing.indexation(pending.version_id, rig.index_id)
    assert indexation["status"] == "CANCELLED"
    assert await rig.indexing.run_index(pending.version_id, rig.index_id) == "skipped"


# --- import de version et retrait concurrents ----------------------------------------


@pytest.mark.parametrize("winner", ["import", "withdraw"])
async def test_a_version_import_and_a_withdrawal_racing_always_end_withdrawn_and_idle(
    rig: S7Rig, winner: str
) -> None:
    # Imports différés : les autres tests de ce fichier passent par l'API et
    # échouent sur leur assertion tant que le retrait n'existe pas.
    from knowledge_base.use_cases.withdraw_document.withdraw_document_command import (
        WithdrawDocument,
    )
    from knowledge_base.use_cases.withdraw_document.withdraw_document_handler import (
        WithdrawDocumentHandler,
    )

    first = await rig.import_extract()
    coordination = Coordination(winner)
    importer = ImportDocumentVersionHandler(
        unit_of_work=lambda: CoordinatedUnitOfWork(
            rig.session_factory, "import", coordination
        ),
        blob_store=FileSystemBlobStore(rig.blob_root),
        max_upload_bytes=64 * 1024,
    )
    withdrawer = WithdrawDocumentHandler(
        unit_of_work=lambda: CoordinatedUnitOfWork(
            rig.session_factory, "withdraw", coordination
        )
    )

    async def import_version() -> object:
        try:
            return await importer.handle(
                ImportDocumentVersion(
                    document_id=first.document_id,
                    filename="fiche.md",
                    media_type="text/markdown",
                    content=_stream(QA_SHEET_V2),
                )
            )
        except DocumentWithdrawnError as refused:
            return refused

    imported, withdrawn = await asyncio.gather(
        import_version(),
        withdrawer.handle(WithdrawDocument(document_id=first.document_id)),
    )

    document = await rig.indexing.document(first.document_id)
    assert document["status"] == "WITHDRAWN"
    assert withdrawn.document_id == first.document_id
    if winner == "withdraw":
        assert isinstance(imported, DocumentWithdrawnError)
    items = await rig.items(first.document_id)
    live = [i for i in items if i["status"] in {"PENDING", "RUNNING"}]
    assert [i["kind"] for i in live] == ["purge_document"], items
    assert [i["kind"] for i in items].count("purge_document") == 1


async def _stream_chunks(data: bytes) -> AsyncIterator[bytes]:
    yield data


def _stream(data: bytes) -> AsyncIterator[bytes]:
    return _stream_chunks(data)
