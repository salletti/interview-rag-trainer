"""Règles de réconciliation ajoutées par S9 (knowledge-base.md §9, §13,
précisions S9 ; décision 8 de l'orchestrateur : règle DISTINCTE de celle des
versions ``EXTRACTED`` de S8, qui reste inchangée).

- ``building_indexes_missing_version`` : un index ``BUILDING`` dont le
  backfill a perdu une version (ligne d'indexation absente) la retrouve ;
  sans cela, l'activation resterait refusée pour toujours (I11). Une version
  ``EXTRACTED`` dont le lot a la politique de l'index relève de la règle de
  S8, jamais des deux.
- ``decide_failed_item`` : un ``rechunk_version`` ``FAILED`` dont le hook n'a
  pas abouti (O14) fait passer l'indexation ``FAILED``, comme
  ``index_version``.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from knowledge_base.domain.entities.indexation import IndexationStatus
from knowledge_base.domain.entities.knowledge_document import (
    DocumentStatus,
    VersionStatus,
)
from knowledge_base.domain.services.reconciliation import (
    FailedItemAction,
    building_indexes_missing_version,
    decide_failed_item,
    indexes_missing_indexation,
)

SAME, SHORT = UUID(int=1), UUID(int=2)
BUILDING = {SAME: "chunking-1+tok", SHORT: "chunking-2+tok"}


def missing(
    version_status: VersionStatus,
    *,
    document_status: DocumentStatus = DocumentStatus.ACTIVE,
    policies: frozenset[str] = frozenset({"chunking-1+tok"}),
    indexed: frozenset[UUID] = frozenset(),
) -> frozenset[UUID]:
    return building_indexes_missing_version(
        document_status=document_status,
        version_status=version_status,
        policy_versions=policies,
        building_policies=BUILDING,
        indexed_index_ids=indexed,
    )


def test_a_published_version_is_missing_from_every_building_index_lacking_it() -> None:
    assert missing(VersionStatus.PUBLISHED) == {SAME, SHORT}
    assert missing(VersionStatus.PUBLISHED, indexed=frozenset({SAME})) == {SHORT}


def test_an_extracted_version_is_missing_only_from_an_index_of_another_policy() -> None:
    assert missing(VersionStatus.EXTRACTED) == {SHORT}


def test_the_s9_rule_never_overlaps_the_s8_rule_for_extracted_versions() -> None:
    s8 = indexes_missing_indexation(
        document_status=DocumentStatus.ACTIVE,
        version_status=VersionStatus.EXTRACTED,
        live_index_ids={SAME},  # index vivant de la politique du lot
        indexed_index_ids=(),
    )
    s9 = missing(VersionStatus.EXTRACTED)

    assert s8 == {SAME}
    assert s8.isdisjoint(s9)


@pytest.mark.parametrize(
    "status",
    [
        VersionStatus.RECEIVED,
        VersionStatus.FAILED,
        VersionStatus.SUPERSEDED,
        VersionStatus.DISCARDED,
    ],
)
def test_a_version_that_cannot_be_published_is_never_backfilled(
    status: VersionStatus,
) -> None:
    assert missing(status) == frozenset()


def test_a_withdrawn_document_is_never_backfilled() -> None:
    assert (
        missing(VersionStatus.PUBLISHED, document_status=DocumentStatus.WITHDRAWN)
        == frozenset()
    )


def test_an_existing_indexation_whatever_its_state_is_a_fact() -> None:
    assert missing(VersionStatus.PUBLISHED, indexed=frozenset(BUILDING)) == (
        frozenset()
    )


def test_without_building_index_nothing_is_missing() -> None:
    assert (
        building_indexes_missing_version(
            document_status=DocumentStatus.ACTIVE,
            version_status=VersionStatus.PUBLISHED,
            policy_versions=frozenset(),
            building_policies={},
            indexed_index_ids=frozenset(),
        )
        == frozenset()
    )


# --- items FAILED (O14) ---------------------------------------------------------------


def test_a_failed_rechunk_whose_hook_never_ran_fails_the_indexation() -> None:
    assert (
        decide_failed_item(
            "rechunk_version", indexation_status=IndexationStatus.PENDING
        )
        is FailedItemAction.FAIL_INDEXATION
    )


@pytest.mark.parametrize(
    "state",
    [IndexationStatus.FAILED, IndexationStatus.COMPLETED, IndexationStatus.CANCELLED],
)
def test_a_failed_rechunk_whose_indexation_moved_on_is_ignored(
    state: IndexationStatus,
) -> None:
    assert (
        decide_failed_item("rechunk_version", indexation_status=state)
        is FailedItemAction.IGNORE
    )


def test_a_failed_rechunk_with_live_work_is_ignored() -> None:
    assert (
        decide_failed_item(
            "rechunk_version",
            indexation_status=IndexationStatus.PENDING,
            live_work=True,
        )
        is FailedItemAction.IGNORE
    )


def test_a_failed_collection_drop_has_no_business_state_to_adopt() -> None:
    assert decide_failed_item("drop_index_collection") is FailedItemAction.IGNORE
