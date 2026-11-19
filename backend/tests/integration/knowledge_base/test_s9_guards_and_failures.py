"""Gardes et échecs de la migration blue/green (S9 ; décisions 5 à 8 de
l'orchestrateur, consignées dans knowledge-base.md §13, précisions S9) sur
PostgreSQL, Qdrant et work queue réels.

- Décision 5 : couverture complète mais synchronisation de visibilité en
  attente (points du nouvel index encore ``visible=false``) → activation
  refusée (``SynchronizationPendingError``) ; activer quand même rendrait une
  recherche vide juste après la bascule.
- Décision 6 : l'item ``drop_index_collection`` n'est disponible qu'après le
  délai ; avant, il n'est pas réclamable et la collection reste.
- Décision 7 : ``rechunk_version`` et ``drop_index_collection`` sont dans le
  catalogue des codes d'erreur ; un échec terminal de rechunking fait passer
  l'indexation ``FAILED`` (hook) et se rejoue par ``/failures`` ; un
  ``drop_index_collection`` d'un index vivant échoue en ``INDEX_STILL_LIVE``,
  non rejouable ; un rechunking ``FAILED`` sans hook est adopté par la
  réconciliation (O14) ; une indexation sans lot de sa politique échoue en
  ``CHUNK_BATCH_MISSING`` au lieu de produire une indexation vide.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from knowledge_base.adapters.controller.composition import (
    build_reconcile_knowledge_base_handler,
)
from knowledge_base.adapters.gateway.blob.filesystem_blob_store import (
    FileSystemBlobStore,
)
from knowledge_base.domain.exceptions.corpus_errors import IncompleteCoverageError
from knowledge_base.domain.services.reconciliation import DiscrepancyKind
from knowledge_base.use_cases.activate_search_index.errors import (
    SynchronizationPendingError,
)
from knowledge_base.use_cases.reconcile_knowledge_base.reconcile_knowledge_base_command import (  # noqa: E501
    ReconcileKnowledgeBase,
)
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.import_support import blob_path
from tests.integration.knowledge_base.s9_support import (
    POLICY_SAME,
    POLICY_SHORT,
    PREFIX,
    S9Rig,
    documents_of,
    long_sheet,
    open_s9_rig,
)
from tests.unit.knowledge_base.use_cases.s9_support import PROFILE_B

pytestmark = pytest.mark.integration


@pytest.fixture
async def rig(
    integration_settings: Settings,
    session_factory: SessionFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[S9Rig]:
    async with open_s9_rig(
        integration_settings, session_factory, tmp_path, monkeypatch
    ) as opened:
        yield opened


async def item(rig: S9Rig, dedup_key: str) -> dict[str, object]:
    (row,) = await rig.rows(
        "SELECT * FROM knowledge_base.work_items WHERE dedup_key = :k", k=dedup_key
    )
    return row


# --- décision 5 : synchronisation en attente ------------------------------------------


async def test_the_activation_waits_for_the_visibility_sync_of_the_backfill(
    rig: S9Rig,
) -> None:
    documents = {
        (await rig.publish(long_sheet(tag), title=f"Fiche {tag}")).document_id
        for tag in ("a", "b")
    }
    new = await rig.start(PROFILE_B, POLICY_SAME)
    # Backfill fait (COMPLETED), points écrits invisibles, synchronisations
    # replanifiées mais pas encore exécutées.
    await rig.drain(index_id=new, kinds={"index_version"})
    assert {i["status"] for i in (await rig.indexations(new)).values()} == {"COMPLETED"}
    pending = await rig.items("sync_visibility")
    assert any(i["status"] == "PENDING" for i in pending.values())

    with pytest.raises(SynchronizationPendingError) as refused:
        await rig.activate(new)

    assert refused.value.document_ids <= documents
    assert await rig.statuses() == {rig.index_id: "ACTIVE", new: "BUILDING"}
    assert await rig.items("drop_index_collection") == {}

    await rig.drain(kinds={"sync_visibility"})
    await rig.activate(new)

    result = await rig.search()
    assert result.index_id == new
    assert documents_of(result) == documents


async def test_an_incomplete_coverage_is_reported_before_the_sync(rig: S9Rig) -> None:
    await rig.publish(long_sheet("a"))
    new = await rig.start(PROFILE_B, POLICY_SAME)

    with pytest.raises(IncompleteCoverageError):
        await rig.activate(new)


# --- décision 6 : suppression différée ------------------------------------------------


async def test_the_old_collection_survives_until_the_delay_is_over(rig: S9Rig) -> None:
    await rig.publish(long_sheet("a"))
    new = await rig.start(PROFILE_B, POLICY_SAME)
    await rig.drain(index_id=new)

    await rig.activate(new, drop_delay=timedelta(seconds=300))

    key = f"drop_index_collection:{rig.index_id}"
    (row,) = await rig.rows(
        "SELECT available_at - now() AS wait, status "
        "FROM knowledge_base.work_items WHERE dedup_key = :k",
        k=key,
    )
    assert row["status"] == "PENDING"
    assert timedelta(seconds=290) < row["wait"] <= timedelta(seconds=300)
    assert await rig.run_key(key) == "skipped"  # pas encore réclamable
    assert await rig.collection_exists(rig.index_id)

    async with rig.session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE knowledge_base.work_items SET available_at = now() "
                "WHERE dedup_key = :k"
            ),
            {"k": key},
        )
    assert await rig.run_key(key) == "succeeded"
    assert not await rig.collection_exists(rig.index_id)


# --- décision 7 : échecs et rejeu -----------------------------------------------------


async def test_a_failed_rechunk_fails_the_indexation_and_is_replayable(
    rig: S9Rig,
) -> None:
    content = long_sheet("a")
    published = await rig.publish(content)
    new = await rig.start(PROFILE_B, POLICY_SHORT)
    path = blob_path(rig.kb.blob_root, content)
    original = path.read_bytes()
    path.unlink()
    key = f"rechunk_version:{published.version_id}:{new}"

    assert await rig.run_key(key) == "failed"

    failed = await item(rig, key)
    assert failed["last_error_code"] == "BLOB_MISSING"
    indexation = (await rig.indexations(new))[published.version_id]
    assert indexation["status"] == "FAILED"
    assert indexation["failure_code"] == "BLOB_MISSING"
    assert indexation["failure_retryable"] is True
    listed = (await rig.http.get(f"{PREFIX}/failures")).json()["items"]
    (entry,) = [i for i in listed if i["kind"] == "rechunk_version"]
    assert entry["replayable"] is True and entry["index_id"] == str(new)

    async def one_chunk() -> AsyncIterator[bytes]:
        yield original

    await FileSystemBlobStore(rig.kb.blob_root).put(one_chunk(), max_bytes=1 << 20)
    response = await rig.http.post(f"{PREFIX}/failures/{failed['id']}/retry")
    assert response.status_code == 202, response.text
    assert (await rig.indexations(new))[published.version_id]["status"] == "PENDING"

    await rig.drain(index_id=new)

    assert (await rig.indexations(new))[published.version_id]["status"] == ("COMPLETED")
    await rig.activate(new)
    assert published.document_id in documents_of(await rig.search())


async def test_dropping_a_live_collection_fails_without_replay(rig: S9Rig) -> None:
    new = await rig.start(PROFILE_B, POLICY_SAME)
    async with rig.session_factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO knowledge_base.work_items (id, kind, dedup_key, payload, "
                "status) VALUES (gen_random_uuid(), 'drop_index_collection', :k, "
                "CAST(:p AS jsonb), 'PENDING')"
            ),
            {"k": f"drop_index_collection:{new}", "p": f'{{"index_id": "{new}"}}'},
        )
    key = f"drop_index_collection:{new}"

    assert await rig.run_key(key) == "failed"

    failed = await item(rig, key)
    assert failed["last_error_code"] == "INDEX_STILL_LIVE"
    response = await rig.http.post(f"{PREFIX}/failures/{failed['id']}/retry")
    assert response.status_code == 409
    assert response.json()["code"] == "NOT_REPLAYABLE"
    assert await rig.collection_exists(new)


async def test_a_failed_drop_of_a_retired_index_is_replayable(rig: S9Rig) -> None:
    new = await rig.start(PROFILE_B, POLICY_SAME)
    await rig.activate(new)
    key = f"drop_index_collection:{rig.index_id}"
    async with rig.session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE knowledge_base.work_items SET status = 'FAILED', "
                "last_error_code = 'TRANSIENT', last_error_message = 'Qdrant', "
                "completed_at = now() WHERE dedup_key = :k"
            ),
            {"k": key},
        )
    failed = await item(rig, key)

    response = await rig.http.post(f"{PREFIX}/failures/{failed['id']}/retry")

    assert response.status_code == 202, response.text
    assert await rig.run_key(key) == "succeeded"
    assert not await rig.collection_exists(rig.index_id)


async def test_a_failed_rechunk_without_hook_is_adopted_by_the_reconciliation(
    rig: S9Rig,
) -> None:
    published = await rig.publish(long_sheet("a"))
    new = await rig.start(PROFILE_B, POLICY_SHORT)
    key = f"rechunk_version:{published.version_id}:{new}"
    async with rig.session_factory() as session, session.begin():
        # Dernière tentative perdue (recover) : item FAILED, hook jamais exécuté.
        await session.execute(
            text(
                "UPDATE knowledge_base.work_items SET status = 'FAILED', "
                "last_error_code = 'LEASE_EXPIRED_MAX_ATTEMPTS', "
                "last_error_message = 'lease expiré', completed_at = now() "
                "WHERE dedup_key = :k"
            ),
            {"k": key},
        )
    assert (await rig.indexations(new))[published.version_id]["status"] == "PENDING"

    report = await build_reconcile_knowledge_base_handler(
        rig.session_factory, rig.vector, rig.kb
    ).handle(ReconcileKnowledgeBase())

    assert report.count(DiscrepancyKind.FAILED_WORK_WITHOUT_STATE) == 1
    indexation = (await rig.indexations(new))[published.version_id]
    assert indexation["status"] == "FAILED"
    assert indexation["failure_code"] == "LEASE_EXPIRED_MAX_ATTEMPTS"


async def test_an_indexation_without_its_batch_fails_instead_of_completing_empty(
    rig: S9Rig,
) -> None:
    published = await rig.publish(long_sheet("a"))
    new = await rig.start(PROFILE_B, POLICY_SHORT)
    key = f"index_version:{published.version_id}:{new}"
    async with rig.session_factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO knowledge_base.work_items (id, kind, dedup_key, payload, "
                "status) VALUES (gen_random_uuid(), 'index_version', :k, "
                "CAST(:p AS jsonb), 'PENDING')"
            ),
            {
                "k": key,
                "p": (
                    f'{{"document_id": "{published.document_id}", '
                    f'"version_id": "{published.version_id}", "index_id": "{new}"}}'
                ),
            },
        )

    assert await rig.run_key(key) == "failed"

    assert (await item(rig, key))["last_error_code"] == "CHUNK_BATCH_MISSING"
    indexation = (await rig.indexations(new))[published.version_id]
    assert indexation["status"] == "FAILED"
    assert await rig.points(new) == []
