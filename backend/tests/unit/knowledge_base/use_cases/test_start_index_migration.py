"""Début d'une migration blue/green : ``ProvisionSearchIndex`` d'un NOUVEL index
``BUILDING`` et backfill du corpus (S9 ; knowledge-base.md §5.4, §13 ; ADR 0011
§4, §7 ; roadmap S9).

Décision de nommage (à valider) : ``ProvisionSearchIndex`` (S4) reste la
commande de DÉMARRAGE de la stack, idempotente, qui crée et active le premier
index d'un corpus vide ; elle ne démarre jamais une migration toute seule (un
profil de configuration différent de l'index actif ne doit pas lancer un
backfill de corpus au redémarrage d'un conteneur). Le démarrage d'une
migration est une commande distincte, ``StartIndexMigration`` (voir
``s9_support`` pour les chemins) ; changer ce nom ne touche que ``s9_support``.

Contrat de ``StartIndexMigrationHandler.handle(command) -> UUID`` (identifiant
du nouvel index) :

1. lit le corpus sous ``FOR UPDATE`` ; refuse sans aucun effet (ni collection,
   ni écriture) : pas d'index actif (``NoActiveIndexToMigrateError``), une
   migration déjà en cours avec un autre profil ou une autre politique
   (``MigrationInProgressError``), un couple (profil, politique) identique à
   celui de l'index actif (``NothingToMigrateError``). Un index ``BUILDING``
   de même couple est rendu tel quel (rejeu sans effet) ;
2. crée la collection Qdrant du profil HORS transaction, AVANT le commit ;
3. dans UNE transaction : index ``BUILDING`` + backfill + commit. Backfill : pour
   chaque version ``PUBLISHED`` ou ``EXTRACTED`` d'un document ``ACTIVE``, une
   ``Indexation PENDING`` portant l'identité de la politique du NOUVEL index et
   un item : ``index_version`` si un lot de chunks de cette politique existe,
   ``rechunk_version`` sinon (O4 : re-parser depuis le blob).
"""

from __future__ import annotations

from uuid import UUID

import pytest

from knowledge_base.domain.entities.indexation import IndexationStatus
from knowledge_base.domain.entities.knowledge_corpus import IndexStatus
from tests.unit.knowledge_base.use_cases.extraction_fakes import StoredBatch
from tests.unit.knowledge_base.use_cases.fakes import NOW
from tests.unit.knowledge_base.use_cases.indexing_fakes import PROFILE, qa_sheet
from tests.unit.knowledge_base.use_cases.s9_support import (
    POLICY_A,
    POLICY_SAME,
    POLICY_SHORT,
    PROFILE_B,
    PROFILE_C,
    S9Rig,
    make_s9_rig,
    start_error,
)


def index_key(version_id: UUID, index_id: UUID) -> str:
    return f"index_version:{version_id}:{index_id}"


def rechunk_key(version_id: UUID, index_id: UUID) -> str:
    return f"rechunk_version:{version_id}:{index_id}"


def document_of(rig: S9Rig, version_id: UUID) -> UUID:
    (found,) = [
        d.document_id
        for d in rig.harness.state.documents.values()
        if any(v.version_id == version_id for v in d.versions)
    ]
    return found


async def populated(rig: S9Rig) -> dict[str, UUID]:
    """Un corpus qui montre chaque cas : une version publiée, une version
    seulement extraite, une version supplantée, un document retiré et une
    version reçue non extraite. Rend les ``version_id`` par nom."""
    await rig.provision()
    published = await rig.publish_document(qa_sheet(2, tag="a"))
    extracted = await rig.extract_only(qa_sheet(2, tag="b"))
    first = await rig.publish_document(qa_sheet(2, tag="c"))
    second = await rig.import_next_version(first.document_id, qa_sheet(3, tag="c"))
    await rig.extract(second)
    assert await rig.index(second, rig.active_id())  # publie v2, supplante v1
    withdrawn = await rig.publish_document(qa_sheet(2, tag="d"))
    rig.document(withdrawn.document_id).withdraw()
    received = await rig.import_document(qa_sheet(2, tag="e"))
    return {
        "published": published.version_id,
        "extracted": extracted.version_id,
        "superseded": first.version_id,
        "republished": second.version_id,
        "withdrawn": withdrawn.version_id,
        "received": received.version_id,
    }


# --- le nouvel index ----------------------------------------------------------------


async def test_a_migration_adds_a_building_index_and_keeps_the_active_one() -> None:
    rig = make_s9_rig()
    active = await rig.provision()

    new = await rig.start()

    assert new != active
    assert rig.statuses() == {active: IndexStatus.ACTIVE, new: IndexStatus.BUILDING}
    assert rig.corpus().active_index_id == active
    index = rig.index_of(new)
    assert (index.profile, index.chunking_policy) == (PROFILE_B, POLICY_SAME)
    assert (index.created_at, index.activated_at, index.retired_at) == (
        NOW,
        None,
        None,
    )


async def test_the_new_collection_is_created_with_the_profile_of_the_new_index() -> (
    None
):
    rig = make_s9_rig()
    active = await rig.provision()

    new = await rig.start()

    assert rig.qdrant.profiles[new] == PROFILE_B
    assert rig.qdrant.profiles[active] == PROFILE
    assert new in rig.qdrant.collections and active in rig.qdrant.collections


async def test_no_qdrant_call_happens_while_a_transaction_is_open() -> None:
    rig = make_s9_rig()
    await populated(rig)

    await rig.start()

    assert rig.violations == []


async def test_the_corpus_is_only_ever_read_under_an_exclusive_lock() -> None:
    rig = make_s9_rig()
    await rig.provision()
    rig.indexing.locks.clear()

    await rig.start()

    # chaque lecture du corpus est FOR UPDATE : la lecture qui précède
    # l'écriture est celle qui verrouille la barrière
    assert rig.indexing.locks
    assert set(rig.indexing.locks) == {"update"}


async def test_provisioning_and_backfill_commit_together() -> None:
    rig = make_s9_rig()
    await populated(rig)
    commits = rig.store.commits

    await rig.start()

    assert rig.store.commits == commits + 1


# --- refus ---------------------------------------------------------------------------


async def test_a_migration_needs_an_active_index() -> None:
    rig = make_s9_rig()

    with pytest.raises(start_error("NoActiveIndexToMigrateError")):
        await rig.start()

    assert rig.indexing.corpus is None
    assert rig.vector.calls == []


async def test_a_second_migration_with_another_profile_is_refused_without_effect() -> (
    None
):
    rig = make_s9_rig()
    await rig.provision()
    building = await rig.start(PROFILE_B, POLICY_SAME)
    calls, statuses = list(rig.vector.calls), rig.statuses()

    with pytest.raises(start_error("MigrationInProgressError")):
        await rig.start(PROFILE_C, POLICY_SAME)

    assert rig.vector.calls == calls
    assert rig.statuses() == statuses
    assert rig.corpus().get_index(building).status is IndexStatus.BUILDING


async def test_a_migration_to_the_profile_and_policy_already_active_is_refused() -> (
    None
):
    rig = make_s9_rig()
    await rig.provision()
    calls = list(rig.vector.calls)

    with pytest.raises(start_error("NothingToMigrateError")):
        await rig.start(PROFILE, POLICY_A)

    assert rig.vector.calls == calls
    assert len(rig.corpus().indexes) == 1


async def test_replaying_the_same_migration_returns_the_same_index_and_writes_nothing() -> (  # noqa: E501
    None
):
    rig = make_s9_rig()
    await populated(rig)
    first = await rig.start()
    calls = list(rig.vector.calls)
    items = dict(rig.harness.state.items)
    indexations = dict(rig.indexing.indexations)

    second = await rig.start()

    assert second == first
    assert rig.vector.calls == calls
    assert rig.harness.state.items == items
    assert rig.indexing.indexations == indexations
    assert len(rig.corpus().indexes) == 2


async def test_a_qdrant_failure_leaves_no_index_in_postgresql() -> None:
    rig = make_s9_rig()
    active = await rig.provision()
    rig.vector.fail_ensure = True

    with pytest.raises(RuntimeError, match="Qdrant"):
        await rig.start()

    assert rig.statuses() == {active: IndexStatus.ACTIVE}


async def test_a_failing_commit_leaves_no_index_indexation_or_item() -> None:
    rig = make_s9_rig()
    await populated(rig)
    statuses = rig.statuses()
    items = dict(rig.harness.state.items)
    indexations = dict(rig.indexing.indexations)
    rig.harness.state.fail_commit = True

    with pytest.raises(RuntimeError, match="commit"):
        await rig.start()
    rig.harness.state.fail_commit = False

    assert rig.statuses() == statuses
    assert rig.harness.state.items == items
    assert rig.indexing.indexations == indexations


# --- backfill ------------------------------------------------------------------------


async def test_the_backfill_indexes_every_published_and_extracted_version() -> None:
    rig = make_s9_rig()
    versions = await populated(rig)

    new = await rig.start()

    in_new = {k[0]: i for k, i in rig.indexing.indexations.items() if k[1] == new}
    assert set(in_new) == {
        versions["published"],
        versions["extracted"],
        versions["republished"],
    }
    for indexation in in_new.values():
        assert indexation.status is IndexationStatus.PENDING
        assert indexation.chunking_policy_version == POLICY_SAME.identity
        assert indexation.manifest_digest is None


async def test_the_backfill_skips_versions_that_cannot_be_published() -> None:
    rig = make_s9_rig()
    versions = await populated(rig)

    new = await rig.start()

    skipped = {versions["superseded"], versions["withdrawn"], versions["received"]}
    assert {k[0] for k in rig.indexing.indexations if k[1] == new}.isdisjoint(skipped)
    scheduled = {
        UUID(key.split(":")[1])
        for key in rig.harness.state.items
        if key.endswith(f":{new}")
    }
    assert scheduled.isdisjoint(skipped)


async def test_the_backfill_enqueues_one_index_version_item_per_version() -> None:
    rig = make_s9_rig()
    versions = await populated(rig)

    new = await rig.start()

    expected = {
        index_key(versions[name], new)
        for name in ("published", "extracted", "republished")
    }
    scheduled = {
        key for key in rig.items_of_kind("index_version") if key.endswith(f":{new}")
    }
    assert scheduled == expected
    for key in expected:
        item = rig.harness.state.items[key]
        version_id = UUID(key.split(":")[1])
        assert item.status == "PENDING"
        assert item.payload == {
            "document_id": str(document_of(rig, version_id)),
            "version_id": str(version_id),
            "index_id": str(new),
        }
    assert rig.items_of_kind("rechunk_version") == {}


async def test_a_different_policy_schedules_a_rechunk_instead_of_an_indexation_item() -> (  # noqa: E501
    None
):
    rig = make_s9_rig()
    versions = await populated(rig)

    new = await rig.start(PROFILE_B, POLICY_SHORT)

    wanted = ("published", "extracted", "republished")
    assert {
        k for k in rig.items_of_kind("rechunk_version") if k.endswith(f":{new}")
    } == {rechunk_key(versions[name], new) for name in wanted}
    assert not [k for k in rig.items_of_kind("index_version") if k.endswith(f":{new}")]
    for name in wanted:
        indexation = rig.indexing.indexations[(versions[name], new)]
        assert indexation.status is IndexationStatus.PENDING
        assert indexation.chunking_policy_version == POLICY_SHORT.identity
        item = rig.harness.state.items[rechunk_key(versions[name], new)]
        assert item.payload == {
            "document_id": str(document_of(rig, versions[name])),
            "version_id": str(versions[name]),
            "index_id": str(new),
        }


async def test_a_version_that_already_has_the_new_policy_batch_skips_the_rechunk() -> (
    None
):
    rig = make_s9_rig()
    versions = await populated(rig)
    rig.store.batches.append(
        StoredBatch(versions["published"], POLICY_SHORT.identity, (), ())
    )

    new = await rig.start(PROFILE_B, POLICY_SHORT)

    assert index_key(versions["published"], new) in rig.harness.state.items
    assert rechunk_key(versions["published"], new) not in rig.harness.state.items
    assert rechunk_key(versions["extracted"], new) in rig.harness.state.items
    assert index_key(versions["extracted"], new) not in rig.harness.state.items


async def test_the_active_index_is_untouched_by_the_backfill() -> None:
    rig = make_s9_rig()
    await populated(rig)
    active = rig.active_id()
    before = {
        k: (i.status, i.manifest_digest)
        for k, i in rig.indexing.indexations.items()
        if k[1] == active
    }
    points = dict(rig.qdrant.points(active))

    await rig.start()

    after = {
        k: (i.status, i.manifest_digest)
        for k, i in rig.indexing.indexations.items()
        if k[1] == active
    }
    assert after == before
    assert dict(rig.qdrant.points(active)) == points
