"""Migration blue/green d'un index de recherche (S9 ; knowledge-base.md §4.1
I9-I11, §13 ; ADR 0011 §3-5, §7 ; roadmap S9) sur PostgreSQL, Qdrant et work
queue réels. Second profil léger (``PROFILE_B``, 24 dimensions) : aucun poids à
télécharger.

Contrat vérifié :

- démarrer une migration crée l'index ``BUILDING`` et SA collection Qdrant (la
  dimension du nouveau profil), pendant que la recherche reste servie par
  l'index actif ;
- le backfill indexe chaque version publiée dans le nouvel index (lot
  d'origine si la politique est la même, lot RECHUNKÉ sinon), points visibles
  AVANT l'activation ;
- une version importée pendant la construction est dans le nouvel index avant
  l'activation (même politique, ou politique différente : rechunking) ;
- l'activation est refusée tant que la couverture est incomplète ; une fois
  faite, la recherche est servie par le nouvel index dès le commit ;
- l'ancien index passe ``RETIRED`` ; sa collection est supprimée par un item
  de maintenance ; plus aucun travail n'est planifié vers lui ;
- un document retiré pendant la construction ne laisse aucun point dans aucun
  des deux index et ne bloque pas l'activation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import pytest

from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    SqlAlchemyWorkQueue,
)
from knowledge_base.domain.exceptions.corpus_errors import IncompleteCoverageError
from knowledge_base.domain.services.point_id import point_id
from knowledge_base.domain.value_objects.chunking_policy import ChunkingPolicy
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.indexing_support import payload
from tests.integration.knowledge_base.s9_support import (
    POLICY_SAME,
    POLICY_SHORT,
    S9Rig,
    documents_of,
    long_sheet,
    open_s9_rig,
)
from tests.unit.knowledge_base.use_cases.indexing_fakes import PROFILE
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


async def corpus_of_three(rig: S9Rig) -> list[UUID]:
    """Trois documents publiés et visibles dans l'index actif."""
    return [
        (await rig.publish(long_sheet(tag), title=f"Fiche {tag}")).document_id
        for tag in ("a", "b", "c")
    ]


# --- démarrage -----------------------------------------------------------------------


async def test_starting_a_migration_creates_the_building_index_and_its_collection(
    rig: S9Rig,
) -> None:
    await corpus_of_three(rig)

    new = await rig.start(PROFILE_B, POLICY_SAME)

    assert await rig.statuses() == {rig.index_id: "ACTIVE", new: "BUILDING"}
    assert await rig.active_id() == rig.index_id
    assert await rig.collection_exists(new)
    assert await rig.vector_size(new) == PROFILE_B.dimension
    assert await rig.vector_size(rig.index_id) == PROFILE.dimension


async def test_the_search_stays_on_the_active_index_during_the_build(
    rig: S9Rig,
) -> None:
    documents = await corpus_of_three(rig)
    new = await rig.start(PROFILE_B, POLICY_SAME)
    await rig.drain(index_id=new)  # backfill complet, pas encore activé

    result = await rig.search()

    assert result.index_id == rig.index_id
    assert documents_of(result) == set(documents)


# --- backfill ------------------------------------------------------------------------


async def test_the_backfill_indexes_every_published_version_into_the_new_index(
    rig: S9Rig,
) -> None:
    documents = await corpus_of_three(rig)
    new = await rig.start(PROFILE_B, POLICY_SAME)
    pending = await rig.indexations(new)
    assert {i["status"] for i in pending.values()} == {"PENDING"}
    assert len(pending) == 3
    before = {r.id for r in await rig.points(rig.index_id)}

    outcomes = await rig.drain(index_id=new)

    assert {o for kind, o in outcomes if kind == "index_version"} == {"succeeded"}
    done = await rig.indexations(new)
    assert {i["status"] for i in done.values()} == {"COMPLETED"}
    for version_id, indexation in done.items():
        expected = await rig.indexing.expected_manifest(version_id, new)
        assert indexation["manifest_digest"] == expected.digest
    assert await rig.point_document_ids(new) == set(documents)
    assert {r.id for r in await rig.points(rig.index_id)} == before


async def test_the_points_of_published_versions_are_visible_before_the_activation(
    rig: S9Rig,
) -> None:
    await corpus_of_three(rig)
    new = await rig.start(PROFILE_B, POLICY_SAME)

    await rig.drain(index_id=new)

    points = await rig.points(new)
    assert points
    assert all(payload(p)["visible"] is True for p in points)


async def test_a_different_policy_is_filled_from_a_rechunked_batch(rig: S9Rig) -> None:
    documents = await corpus_of_three(rig)
    new = await rig.start(PROFILE_B, POLICY_SHORT)
    rechunks = await rig.items("rechunk_version")
    assert len(rechunks) == 3

    await rig.drain(index_id=new)

    versions = [v["version_id"] for v in (await rig.indexations(new)).values()]
    for version_id in versions:
        assert await rig.chunk_count(version_id, POLICY_SAME) == 1
        assert await rig.chunk_count(version_id, POLICY_SHORT) > 1
        chunks = await rig.indexing.extraction.chunks(version_id)
        short = [
            c for c in chunks if c["chunking_policy_version"] == POLICY_SHORT.identity
        ]
        expected = {point_id(new, c["id"]) for c in short}
        in_new = {
            UUID(str(p.id))
            for p in await rig.points(new)
            if payload(p)["version_id"] == str(version_id)
        }
        assert in_new == expected
    assert await rig.point_document_ids(new) == set(documents)
    assert {i["status"] for i in (await rig.indexations(new)).values()} == {"COMPLETED"}


# --- une version importée pendant la construction ------------------------------------


@pytest.mark.parametrize("policy", [POLICY_SAME, POLICY_SHORT], ids=["same", "short"])
async def test_a_version_imported_during_the_build_reaches_the_new_index(
    rig: S9Rig, policy: ChunkingPolicy
) -> None:
    await corpus_of_three(rig)
    new = await rig.start(PROFILE_B, policy)
    late = await rig.import_extract(long_sheet("late"), title="Fiche tardive")
    assert await rig.run_index(late.version_id, rig.index_id) == "succeeded"
    await rig.drain(index_id=new)  # backfill et import tardif

    assert late.document_id in await rig.point_document_ids(new)
    indexation = (await rig.indexations(new))[late.version_id]
    assert indexation["status"] == "COMPLETED"
    await rig.activate(new)
    result = await rig.search()
    assert result.index_id == new
    assert late.document_id in documents_of(result)


# --- l'activation --------------------------------------------------------------------


async def test_the_activation_is_refused_until_the_backfill_is_complete(
    rig: S9Rig,
) -> None:
    await corpus_of_three(rig)
    new = await rig.start(PROFILE_B, POLICY_SAME)

    with pytest.raises(IncompleteCoverageError):
        await rig.activate(new)

    assert await rig.statuses() == {rig.index_id: "ACTIVE", new: "BUILDING"}
    assert await rig.active_id() == rig.index_id
    assert await rig.items("drop_index_collection") == {}

    await rig.drain(index_id=new)
    await rig.activate(new)

    assert await rig.statuses() == {rig.index_id: "RETIRED", new: "ACTIVE"}


async def test_the_search_is_served_by_the_new_index_right_after_the_commit(
    rig: S9Rig,
) -> None:
    documents = await corpus_of_three(rig)
    new = await rig.start(PROFILE_B, POLICY_SAME)
    await rig.drain(index_id=new)

    await rig.activate(new)

    result = await rig.search()  # aucun item exécuté entre le commit et la requête
    assert result.index_id == new
    assert documents_of(result) == set(documents)
    assert result.complete is True


async def test_the_switch_keeps_one_active_index_and_retires_the_old_one(
    rig: S9Rig,
) -> None:
    await corpus_of_three(rig)
    new = await rig.start(PROFILE_B, POLICY_SAME)
    await rig.drain(index_id=new)

    await rig.activate(new)

    assert await rig.statuses() == {rig.index_id: "RETIRED", new: "ACTIVE"}
    assert await rig.active_id() == new
    (retired,) = await rig.rows(
        "SELECT retired_at FROM knowledge_base.search_indexes WHERE index_id = :i",
        i=rig.index_id,
    )
    assert retired["retired_at"] is not None


# --- retrait de l'ancien index -------------------------------------------------------


async def test_the_old_collection_is_deleted_by_its_work_item_after_the_switch(
    rig: S9Rig,
) -> None:
    documents = await corpus_of_three(rig)
    new = await rig.start(PROFILE_B, POLICY_SAME)
    await rig.drain(index_id=new)
    await rig.activate(new)
    key = f"drop_index_collection:{rig.index_id}"
    (item,) = (await rig.items("drop_index_collection")).values()
    assert item["dedup_key"] == key and item["status"] == "PENDING"
    assert await rig.collection_exists(rig.index_id)  # l'activation n'y touche pas

    assert await rig.run_key(key) == "succeeded"

    assert not await rig.collection_exists(rig.index_id)
    assert await rig.collection_exists(new)
    assert await rig.point_document_ids(new) == set(documents)
    assert documents_of(await rig.search()) == set(documents)
    assert (await rig.statuses())[rig.index_id] == "RETIRED"


async def test_the_work_item_never_deletes_a_live_collection(rig: S9Rig) -> None:
    await corpus_of_three(rig)
    new = await rig.start(PROFILE_B, POLICY_SAME)
    await rig.drain(index_id=new)
    async with rig.session_factory() as session, session.begin():
        await SqlAlchemyWorkQueue(session).enqueue(
            "drop_index_collection",
            f"drop_index_collection:{new}",
            {"index_id": str(new)},
        )

    outcome = await rig.run_key(f"drop_index_collection:{new}")

    assert outcome == "failed"  # refus terminal : rejouer ne changerait rien
    assert await rig.collection_exists(new)
    assert await rig.collection_exists(rig.index_id)


async def test_nothing_is_planned_towards_the_retired_index(rig: S9Rig) -> None:
    await corpus_of_three(rig)
    pending = await rig.import_extract(long_sheet("p"), title="En cours")
    new = await rig.start(PROFILE_B, POLICY_SAME)
    await rig.drain(index_id=new)
    await rig.activate(new)
    old_points = {r.id for r in await rig.points(rig.index_id)}

    # le travail d'indexation resté en attente dans l'ancien index s'éteint
    await rig.drain(index_id=rig.index_id)
    after = await rig.import_extract(long_sheet("z"), title="Après")

    assert {r.id for r in await rig.points(rig.index_id)} == old_points
    assert set(await rig.indexations(rig.index_id)).isdisjoint({after.version_id})
    assert after.version_id in await rig.indexations(new)
    assert not [
        k
        for k in await rig.items("index_version")
        if k.endswith(f":{rig.index_id}") and str(after.version_id) in k
    ]
    assert pending.version_id in await rig.indexations(new)


# --- un document retiré pendant la construction --------------------------------------


async def test_a_document_withdrawn_during_the_build_leaves_no_point_and_blocks_nothing(
    rig: S9Rig,
) -> None:
    documents = await corpus_of_three(rig)
    new = await rig.start(PROFILE_B, POLICY_SAME)
    withdrawn = documents[0]
    await rig.withdraw(withdrawn)

    await rig.drain(index_id=new, kinds={"index_version", "sync_visibility"})
    await rig.drain(kinds={"purge_document"})
    await rig.activate(new)

    assert withdrawn not in await rig.point_document_ids(new)
    assert withdrawn not in await rig.point_document_ids(rig.index_id)
    result = await rig.search()
    assert result.index_id == new
    assert documents_of(result) == set(documents[1:])
