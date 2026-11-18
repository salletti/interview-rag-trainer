"""Réindexation d'une version déjà publiée (S8, décision 8 de l'orchestrateur ;
knowledge-base.md §9, §11) : la réconciliation rouvre une indexation dont le
manifest ne se vérifie plus ; ``prepare`` réécrit les points INVISIBLES, et la
publication, déjà faite, ne planifie rien. La finalisation de ``index_version``
replanifie donc ``sync_visibility``, sous une nouvelle génération de la clé de
la révision courante si sa synchronisation a déjà été exécutée.

Revue S8 (I3) : pour une version déjà publiée, ``prepare`` ne réécrit que les
points absents ou d'empreinte différente ; les points corrects restent
visibles (le document reste trouvable pendant la réparation).
"""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest

from knowledge_base.domain.entities.indexation import IndexationStatus
from knowledge_base.domain.entities.knowledge_document import VersionStatus
from knowledge_base.use_cases.record_extraction.record_extraction_command import (
    RecordExtraction,
)
from tests.unit.knowledge_base.use_cases.indexing_fakes import (
    IndexingRig,
    make_indexing_rig,
    qa_sheet,
)


def synchronized(rig: IndexingRig, command: RecordExtraction, index_id: UUID) -> None:
    """Points de la version visibles, comme après ``sync_visibility``."""
    collection = rig.vector.points(index_id)
    for key, point in list(collection.items()):
        if point.version_id == command.version_id:
            collection[key] = replace(point, visible=True)


async def published(rig: IndexingRig) -> tuple[RecordExtraction, UUID]:
    index_id = await rig.provision()
    command = await rig.import_document(qa_sheet(2))
    await rig.extract(command)
    assert await rig.index(command, index_id) is True
    assert rig.status(command) is VersionStatus.PUBLISHED
    return command, index_id


async def test_reindexing_a_published_version_schedules_a_new_synchronization() -> None:
    rig = make_indexing_rig()
    command, index_id = await published(rig)
    key = f"sync_visibility:{command.document_id}:1"
    rig.harness.state.items[key].status = "SUCCEEDED"  # synchronisation faite
    rig.indexation(command, index_id).reopen()

    assert await rig.index(command, index_id) is True

    assert rig.indexation(command, index_id).status is IndexationStatus.COMPLETED
    assert rig.status(command) is VersionStatus.PUBLISHED
    assert rig.sync_keys() == [key, f"{key}:2"]
    assert rig.harness.state.items[f"{key}:2"].status == "PENDING"


async def test_a_synchronization_still_pending_is_not_duplicated() -> None:
    rig = make_indexing_rig()
    command, index_id = await published(rig)
    rig.indexation(command, index_id).reopen()

    await rig.index(command, index_id)

    key = f"sync_visibility:{command.document_id}:1"
    assert rig.sync_keys() == [key]


@pytest.mark.parametrize("damage", ["lost_point", "wrong_hash"])
async def test_only_the_damaged_point_of_a_published_version_is_rewritten(
    damage: str,
) -> None:
    rig = make_indexing_rig()
    index_id = await rig.provision()
    command = await rig.import_document(qa_sheet(3))
    await rig.extract(command)
    assert await rig.index(command, index_id) is True
    synchronized(rig, command, index_id)
    collection = rig.vector.points(index_id)
    damaged, *intact = sorted(p.point_id for p in rig.points_of(command, index_id))
    if damage == "lost_point":
        del collection[damaged]
    else:
        collection[damaged] = replace(collection[damaged], chunk_hash="0" * 64)
    rig.indexation(command, index_id).reopen()
    sizes_before = list(rig.vector.upsert_sizes)

    assert await rig.index(command, index_id) is True

    assert rig.vector.upsert_sizes[len(sizes_before) :] == [1]
    assert all(collection[point].visible for point in intact)
    assert collection[damaged].visible is False  # jusqu'à la synchronisation
    assert collection[damaged].chunk_hash != "0" * 64
    assert rig.indexation(command, index_id).status is IndexationStatus.COMPLETED


async def test_an_unpublished_version_is_still_rewritten_entirely() -> None:
    """Points invisibles de toute façon : la règle de S4 (tout réécrire à
    chaque tentative) reste celle d'une version non publiée."""
    rig = make_indexing_rig()
    index_id = await rig.provision()
    command = await rig.import_document(qa_sheet(3))
    await rig.extract(command)
    await rig.index(command, index_id, commit=False)  # tentative perdue
    assert rig.status(command) is VersionStatus.EXTRACTED
    assert rig.vector.upsert_sizes == [3]

    assert await rig.index(command, index_id) is True

    assert rig.vector.upsert_sizes == [3, 3]
