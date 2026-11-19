"""Gardes de l'activation ajoutées par les décisions de S9 (knowledge-base.md
§13, précisions S9) : synchronisation de visibilité en attente et suppression
différée de l'ancienne collection.

- Décision 5 : couverture complète, mais une synchronisation de visibilité
  en attente pour un document publié (points du candidat encore
  ``visible=false``) → ``SynchronizationPendingError``, sans aucun effet. Un
  document seulement extrait ne bloque pas.
- Revue S9 (I4) : de même si la DERNIÈRE synchronisation d'un document publié
  est ``FAILED`` (rien n'a convergé ses points depuis) →
  ``SynchronizationFailedError``, sans aucun effet ; elle se débloque par le
  rejeu de l'échec (``/failures``) ou par la réconciliation (points
  invisibles d'une version publiée), qui replanifient la synchronisation.
- Décision 6 : l'item ``drop_index_collection`` est disponible après
  ``drop_delay`` (``KB_INDEX_DROP_DELAY_SECONDS`` en production, 300 s par
  défaut) ; un délai négatif est refusé à la construction, et le délai n'a
  pas de valeur par défaut (revue S9, M2).
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest

from knowledge_base.domain.entities.knowledge_corpus import IndexStatus
from knowledge_base.domain.exceptions.corpus_errors import IncompleteCoverageError
from knowledge_base.frameworks.configuration.settings import KnowledgeBaseSettings
from knowledge_base.use_cases.activate_search_index.activate_search_index_command import (  # noqa: E501
    ActivateSearchIndex,
)
from knowledge_base.use_cases.activate_search_index.activate_search_index_handler import (  # noqa: E501
    ActivateSearchIndexHandler,
)
from knowledge_base.use_cases.activate_search_index.errors import (
    SynchronizationFailedError,
    SynchronizationPendingError,
)
from tests.unit.knowledge_base.use_cases.indexing_fakes import qa_sheet
from tests.unit.knowledge_base.use_cases.s9_support import LATER, S9Rig, make_s9_rig


async def covered(rig: S9Rig) -> tuple[UUID, UUID]:
    """Index actif, deux documents publiés, nouvel index couvert."""
    old = await rig.provision()
    for tag in "ab":
        await rig.publish_document(qa_sheet(2, tag=tag))
    new = await rig.start()
    await rig.index_published_into(new)
    return old, new


async def test_a_pending_visibility_sync_blocks_the_activation() -> None:
    rig = make_s9_rig()
    old = await rig.provision()
    published = await rig.publish_document(qa_sheet(2, tag="a"))
    new = await rig.start()
    await rig.index_published_into(new)
    rig.indexing.awaiting_visibility = frozenset({published.document_id})
    commits = rig.store.commits

    with pytest.raises(SynchronizationPendingError) as refused:
        await rig.activate(new)

    assert refused.value.document_ids == {published.document_id}
    assert refused.value.index_id == new
    assert rig.store.commits == commits
    assert rig.statuses() == {old: IndexStatus.ACTIVE, new: IndexStatus.BUILDING}
    assert rig.items_of_kind("drop_index_collection") == {}


async def test_the_activation_passes_once_the_sync_is_done() -> None:
    rig = make_s9_rig()
    old = await rig.provision()
    published = await rig.publish_document(qa_sheet(2, tag="a"))
    new = await rig.start()
    await rig.index_published_into(new)
    rig.indexing.awaiting_visibility = frozenset({published.document_id})
    with pytest.raises(SynchronizationPendingError):
        await rig.activate(new)

    rig.indexing.awaiting_visibility = frozenset()
    await rig.activate(new)

    assert rig.statuses() == {old: IndexStatus.RETIRED, new: IndexStatus.ACTIVE}


async def test_a_pending_sync_of_an_unpublished_document_does_not_block() -> None:
    rig = make_s9_rig()
    old = await rig.provision()
    await rig.publish_document(qa_sheet(2, tag="a"))
    extracted = await rig.extract_only(qa_sheet(2, tag="b"))
    new = await rig.start()
    await rig.index_published_into(new)
    rig.indexing.awaiting_visibility = frozenset({extracted.document_id})

    await rig.activate(new)

    assert rig.statuses() == {old: IndexStatus.RETIRED, new: IndexStatus.ACTIVE}


async def test_a_failed_visibility_sync_blocks_the_activation() -> None:
    rig = make_s9_rig()
    old = await rig.provision()
    published = await rig.publish_document(qa_sheet(2, tag="a"))
    new = await rig.start()
    await rig.index_published_into(new)
    rig.indexing.failed_visibility = frozenset({published.document_id})
    commits = rig.store.commits

    with pytest.raises(SynchronizationFailedError) as refused:
        await rig.activate(new)

    assert refused.value.document_ids == {published.document_id}
    assert refused.value.index_id == new
    assert "failures" in str(refused.value)
    assert rig.store.commits == commits
    assert rig.statuses() == {old: IndexStatus.ACTIVE, new: IndexStatus.BUILDING}
    assert rig.items_of_kind("drop_index_collection") == {}


async def test_a_failed_sync_of_an_unpublished_document_does_not_block() -> None:
    rig = make_s9_rig()
    old = await rig.provision()
    await rig.publish_document(qa_sheet(2, tag="a"))
    extracted = await rig.extract_only(qa_sheet(2, tag="b"))
    new = await rig.start()
    await rig.index_published_into(new)
    rig.indexing.failed_visibility = frozenset({extracted.document_id})

    await rig.activate(new)

    assert rig.statuses() == {old: IndexStatus.RETIRED, new: IndexStatus.ACTIVE}


async def test_an_incomplete_coverage_is_reported_before_a_failed_sync() -> None:
    rig = make_s9_rig()
    await rig.provision()
    published = await rig.publish_document(qa_sheet(2, tag="a"))
    new = await rig.start()  # rien d'indexé dans le nouvel index
    rig.indexing.failed_visibility = frozenset({published.document_id})

    with pytest.raises(IncompleteCoverageError):
        await rig.activate(new)


async def test_an_incomplete_coverage_is_reported_before_a_pending_sync() -> None:
    rig = make_s9_rig()
    await rig.provision()
    published = await rig.publish_document(qa_sheet(2, tag="a"))
    new = await rig.start()  # rien d'indexé dans le nouvel index
    rig.indexing.awaiting_visibility = frozenset({published.document_id})

    with pytest.raises(IncompleteCoverageError):
        await rig.activate(new)


async def test_the_backfill_never_relies_on_a_sync_already_running() -> None:
    """Un ``sync_visibility`` RUNNING a pu converger le candidat avant que le
    backfill y écrive les points (invisibles) : la replanification crée la
    génération suivante, que l'activation attendra."""
    rig = make_s9_rig()
    await rig.provision()
    published = await rig.publish_document(qa_sheet(2, tag="a"))
    (key,) = rig.items_of_kind("sync_visibility")
    rig.harness.state.items[key].status = "RUNNING"
    new = await rig.start()

    await rig.index_published_into(new)

    syncs = {k: i.status for k, i in rig.items_of_kind("sync_visibility").items()}
    assert syncs == {key: "RUNNING", f"{key}:2": "PENDING"}
    assert str(published.document_id) in key


# --- suppression différée -------------------------------------------------------------


async def test_the_drop_item_waits_for_the_configured_delay() -> None:
    rig = make_s9_rig()
    old, new = await covered(rig)
    handler = ActivateSearchIndexHandler(
        unit_of_work=rig.unit_of_work,
        clock=lambda: LATER,
        drop_delay=timedelta(seconds=300),
    )

    await handler.handle(ActivateSearchIndex(index_id=new))

    item = rig.harness.state.items[f"drop_index_collection:{old}"]
    assert item.delay == timedelta(seconds=300)
    assert item.status == "PENDING"


async def test_with_a_zero_delay_the_drop_is_available_at_once() -> None:
    rig = make_s9_rig()
    old, new = await covered(rig)

    await rig.activate(new)

    assert rig.harness.state.items[f"drop_index_collection:{old}"].delay == (
        timedelta(0)
    )


def test_a_negative_delay_is_refused() -> None:
    rig = make_s9_rig()

    with pytest.raises(ValueError, match="drop_delay"):
        ActivateSearchIndexHandler(
            unit_of_work=rig.unit_of_work,
            clock=lambda: LATER,
            drop_delay=timedelta(seconds=-1),
        )


def test_the_delay_has_no_default() -> None:
    rig = make_s9_rig()

    with pytest.raises(TypeError, match="drop_delay"):
        ActivateSearchIndexHandler(  # type: ignore[call-arg]
            unit_of_work=rig.unit_of_work, clock=lambda: LATER
        )


def test_the_production_delay_defaults_to_five_minutes() -> None:
    settings = KnowledgeBaseSettings(_env_file=None)

    assert settings.index_drop_delay == timedelta(seconds=300)
