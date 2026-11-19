"""La réconciliation de S8 et les workers couvrent le nouvel index (S9 ; roadmap
« réconciliation S8 couvre le nouvel index » ; knowledge-base.md §9, §13 ;
reports S4 M3/M4) sur PostgreSQL, Qdrant et work queue réels.

Contrat vérifié :

- une version ``PUBLISHED`` ou ``EXTRACTED`` qui a perdu son indexation dans un
  index ``BUILDING`` (ligne et item perdus) est retrouvée par un cycle de
  réconciliation, puis indexée : sans cela l'activation resterait refusée pour
  toujours ; même chose quand l'index est d'une autre politique (le cycle
  replanifie alors le rechunking) ;
- un point orphelin d'un index ``BUILDING`` est supprimé comme dans l'index
  actif ; une collection retirée puis supprimée n'est pas un écart ;
- une version indexée dans le nouvel index AVANT l'activation, jamais publiée
  parce que l'ancien index n'avait pas fini, est publiée par la réconciliation
  une fois l'index activé (publication perdue) ;
- un worker qui ne sait pas charger le profil d'un index échoue en
  ``COLLECTION_MISMATCH`` terminal, sans rien écrire : ce n'est plus le sort de
  tout index d'un autre profil (report S4 M3).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from knowledge_base.adapters.controller.composition import (
    build_reconcile_knowledge_base_handler,
)
from knowledge_base.domain.value_objects.chunking_policy import ChunkingPolicy
from knowledge_base.domain.value_objects.indexed_point import IndexedPoint
from knowledge_base.use_cases.reconcile_knowledge_base.reconcile_knowledge_base_command import (  # noqa: E501
    ReconcileKnowledgeBase,
)
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.s9_support import (
    POLICY_SAME,
    POLICY_SHORT,
    S9Rig,
    documents_of,
    long_sheet,
    open_s9_rig,
)
from tests.unit.knowledge_base.use_cases.indexing_fakes import PROFILE, hash_vector
from tests.unit.knowledge_base.use_cases.s9_support import PROFILE_B, ProfileModel

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


async def cycle(rig: S9Rig) -> Any:
    handler = build_reconcile_knowledge_base_handler(
        rig.session_factory, rig.vector, rig.kb
    )
    return await handler.handle(ReconcileKnowledgeBase())


async def converge(rig: S9Rig, *, cycles: int = 8) -> None:
    """Cycle, exécution du travail planifié, jusqu'à un cycle propre."""
    for _ in range(cycles):
        report = await cycle(rig)
        if not await rig.drain() and report.clean:
            return
    raise AssertionError("la réconciliation ne converge pas")


async def lose_indexation(rig: S9Rig, version_id: UUID, index_id: UUID) -> None:
    """La ligne d'indexation et ses items (indexation, rechunking) disparaissent."""
    async with rig.session_factory() as session, session.begin():
        await session.execute(
            text(
                "DELETE FROM knowledge_base.indexations "
                "WHERE version_id = :v AND index_id = :i"
            ),
            {"v": version_id, "i": index_id},
        )
        await session.execute(
            text(
                "DELETE FROM knowledge_base.work_items "
                "WHERE kind IN ('index_version', 'rechunk_version') "
                "AND payload->>'version_id' = :v AND payload->>'index_id' = :i"
            ),
            {"v": str(version_id), "i": str(index_id)},
        )


# --- versions sans indexation dans un index BUILDING ---------------------------------


@pytest.mark.parametrize("policy", [POLICY_SAME, POLICY_SHORT], ids=["same", "short"])
async def test_a_published_version_without_indexation_in_the_new_index_is_recovered(
    rig: S9Rig, policy: ChunkingPolicy
) -> None:
    published = await rig.publish(long_sheet("a"))
    new = await rig.start(PROFILE_B, policy)
    await lose_indexation(rig, published.version_id, new)
    assert published.version_id not in await rig.indexations(new)

    await converge(rig)

    recovered = (await rig.indexations(new))[published.version_id]
    assert recovered["status"] == "COMPLETED"
    assert published.document_id in await rig.point_document_ids(new)
    await rig.activate(new)  # la couverture est complète


@pytest.mark.parametrize("policy", [POLICY_SAME, POLICY_SHORT], ids=["same", "short"])
async def test_an_extracted_version_without_indexation_in_the_new_index_is_recovered(
    rig: S9Rig, policy: ChunkingPolicy
) -> None:
    extracted = await rig.import_extract(long_sheet("a"))
    new = await rig.start(PROFILE_B, policy)
    await lose_indexation(rig, extracted.version_id, new)

    await converge(rig)

    assert (await rig.indexations(new))[extracted.version_id]["status"] == "COMPLETED"


async def test_an_orphan_point_of_the_building_index_is_deleted(rig: S9Rig) -> None:
    await rig.publish(long_sheet("a"))
    new = await rig.start(PROFILE_B, POLICY_SAME)
    await rig.drain(index_id=new)
    ghost = UUID(int=999_001)
    await rig.vector.upsert(
        new,
        [
            IndexedPoint(
                point_id=UUID(int=999_002),
                vector=tuple(hash_vector("fantôme", PROFILE_B.dimension)),
                chunk_id=UUID(int=999_003),
                unit_id=UUID(int=999_004),
                version_id=UUID(int=999_005),
                document_id=ghost,
                chunk_hash="0" * 64,
                index_id=new,
                topics=(),
                kind="QA_SHEET",
                language="fr",
            )
        ],
    )
    assert ghost in await rig.point_document_ids(new)

    await converge(rig)

    assert ghost not in await rig.point_document_ids(new)


async def test_a_dropped_collection_is_not_a_discrepancy(rig: S9Rig) -> None:
    await rig.publish(long_sheet("a"))
    new = await rig.start(PROFILE_B, POLICY_SAME)
    await rig.drain(index_id=new)
    await rig.activate(new)
    assert await rig.run_key(f"drop_index_collection:{rig.index_id}") == "succeeded"

    await converge(rig)

    assert not await rig.collection_exists(rig.index_id)
    assert await rig.collection_exists(new)


async def test_a_version_completed_in_the_new_index_is_published_after_the_switch(
    rig: S9Rig,
) -> None:
    first = await rig.publish(long_sheet("a"))
    new = await rig.start(PROFILE_B, POLICY_SAME)
    await rig.drain(index_id=new)
    response = await rig.indexing.extraction.imports.post_version(
        first.document_id, long_sheet("a2")
    )
    version = UUID(response.json()["version_id"])
    (extract,) = [
        i
        for i in await rig.indexing.extraction.imports.extract_items()
        if i["payload"]["version_id"] == str(version)
    ]
    assert await rig.run_id(extract["id"]) == "succeeded"
    # v2 est terminée dans le NOUVEL index seulement : l'ancien (actif) n'a pas
    # fini, la publication est refusée (I9).
    await rig.drain(index_id=new)
    (document,) = await rig.indexing.extraction.imports.documents()
    assert document["published_version_id"] == first.version_id
    await rig.activate(new)  # v2 n'est pas publiée : la couverture ne l'exige pas

    await converge(rig)

    (document,) = await rig.indexing.extraction.imports.documents()
    assert document["published_version_id"] == version
    result = await rig.search()
    assert result.index_id == new
    assert {p.citation.version_id for p in result.passages} == {version}
    assert documents_of(result) == {first.document_id}


# --- workers --------------------------------------------------------------------------


async def test_the_worker_cannot_load_the_profile_of_the_index_and_fails_terminally(
    integration_settings: Settings,
    session_factory: SessionFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with open_s9_rig(
        integration_settings,
        session_factory,
        tmp_path,
        monkeypatch,
        models=lambda: (ProfileModel(PROFILE),),  # ce worker ne charge pas B
    ) as rig:
        published = await rig.publish(long_sheet("a"))
        new = await rig.start(PROFILE_B, POLICY_SAME)

        outcome = await rig.run_index(published.version_id, new)

        assert outcome == "failed"
        item = await rig.items("index_version")
        failed = item[f"index_version:{published.version_id}:{new}"]
        assert failed["last_error_code"] == "COLLECTION_MISMATCH"
        indexation = (await rig.indexations(new))[published.version_id]
        assert indexation["status"] == "FAILED"
        assert indexation["failure_code"] == "COLLECTION_MISMATCH"
        assert await rig.points(new) == []
