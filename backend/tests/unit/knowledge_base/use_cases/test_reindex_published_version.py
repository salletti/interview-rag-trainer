"""Réindexation d'une version déjà publiée (S8, décision 8 de l'orchestrateur ;
knowledge-base.md §9, §11) : la réconciliation rouvre une indexation dont le
manifest ne se vérifie plus ; ``prepare`` réécrit les points INVISIBLES, et la
publication, déjà faite, ne planifie rien. La finalisation de ``index_version``
replanifie donc ``sync_visibility``, sous une nouvelle génération de la clé de
la révision courante si sa synchronisation a déjà été exécutée.
"""

from __future__ import annotations

from uuid import UUID

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
