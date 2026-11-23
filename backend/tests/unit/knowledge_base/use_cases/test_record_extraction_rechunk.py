"""Une extraction qui se termine pendant la construction d'un index d'une AUTRE
politique de chunking lui prépare son rechunking (S9 ; knowledge-base.md §13 :
« une extraction qui se termine pendant la construction voit forcément l'index
BUILDING » ; ADR 0011 §5, §7). Remplace le comportement de S4 qui sautait
l'index (journal « version non indexée dans … : politique de l'index … »).

Contrat de ``RecordExtractionHandler.apply`` (même transaction, corpus lu sous
``FOR SHARE``) : pour chaque index vivant,

- politique identique à celle du lot : ``Indexation PENDING`` et item
  ``index_version`` (inchangé) ;
- politique différente : ``Indexation PENDING`` portant l'identité de la
  politique de l'INDEX et item ``rechunk_version`` (dedup ``rechunk_version:
  <version_id>:<index_id>``, mêmes champs de payload que ``index_version``), au
  lieu d'ignorer l'index ; le lot écrit reste celui de la politique de
  l'extraction. Aucun ``EXTRACTED_WITHOUT_INDEXATION`` : la version est couverte.
"""

from __future__ import annotations

import logging

import pytest

from knowledge_base.domain.entities.indexation import IndexationStatus
from knowledge_base.use_cases.record_extraction.record_extraction_handler import (
    EXTRACTED_WITHOUT_INDEXATION,
)
from tests.unit.knowledge_base.use_cases.indexing_fakes import qa_sheet
from tests.unit.knowledge_base.use_cases.s9_support import (
    POLICY_A,
    POLICY_SHORT,
    PROFILE_B,
    make_s9_rig,
)


async def test_an_index_of_another_policy_gets_a_pending_indexation_and_a_rechunk() -> (
    None
):
    rig = make_s9_rig()
    active = await rig.provision()
    building = await rig.start(PROFILE_B, POLICY_SHORT)

    command = await rig.extract_only(qa_sheet(2, tag="a"))

    indexation = rig.indexing.indexations[(command.version_id, building)]
    assert indexation.status is IndexationStatus.PENDING
    assert indexation.chunking_policy_version == POLICY_SHORT.identity
    key = f"rechunk_version:{command.version_id}:{building}"
    assert rig.harness.state.items[key].kind == "rechunk_version"
    assert rig.harness.state.items[key].status == "PENDING"
    assert rig.harness.state.items[key].payload == {
        "document_id": str(command.document_id),
        "version_id": str(command.version_id),
        "index_id": str(building),
    }
    # l'index actif, de la politique du lot, est indexé directement
    assert f"index_version:{command.version_id}:{active}" in rig.harness.state.items
    assert f"index_version:{command.version_id}:{building}" not in (
        rig.harness.state.items
    )


async def test_only_the_batch_of_the_extraction_policy_is_written() -> None:
    rig = make_s9_rig()
    await rig.provision()
    await rig.start(PROFILE_B, POLICY_SHORT)

    command = await rig.extract_only(qa_sheet(2, tag="a"))

    assert set(rig.batches_of(command)) == {POLICY_A.identity}


async def test_a_version_covered_by_a_rechunk_is_not_reported_as_unindexed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = make_s9_rig()
    await rig.provision()
    await rig.start(PROFILE_B, POLICY_SHORT)

    with caplog.at_level(logging.WARNING):
        await rig.extract_only(qa_sheet(2, tag="a"))

    assert EXTRACTED_WITHOUT_INDEXATION not in caplog.text


async def test_a_retired_index_of_another_policy_gets_nothing() -> None:
    rig = make_s9_rig()
    await rig.provision()
    abandoned = await rig.start(PROFILE_B, POLICY_SHORT)
    rig.corpus().retire_index(abandoned, now=rig.index_of(abandoned).created_at)

    command = await rig.extract_only(qa_sheet(2, tag="a"))

    assert (command.version_id, abandoned) not in rig.indexing.indexations
    assert not [k for k in rig.harness.state.items if k.startswith("rechunk_version")]
