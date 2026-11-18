"""Compléments S8 aux règles de réconciliation (décisions 6 et 12 de
l'orchestrateur) :

- réouverture BORNÉE d'une purge ``FAILED`` : au-delà de ``max_reopenings``
  réouvertures (``reopen_count``), la purge attend un rejeu explicite
  (``KEEP_FAILED_PURGE``), sans boucle au rythme du cycle quand Qdrant est
  durablement en panne ;
- un item du même travail encore en attente ou en cours possède l'état
  métier : l'item ``FAILED`` est ignoré ;
- « points en trop » d'une version vérifiée (§9) : ceux qui sont absents de
  son manifest.
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
    decide_failed_item,
    points_beyond_manifest,
)
from knowledge_base.domain.value_objects.error_catalog import is_known_replayable
from knowledge_base.domain.value_objects.index_manifest import IndexManifest

W = DocumentStatus.WITHDRAWN


@pytest.mark.parametrize(
    ("reopen_count", "max_reopenings", "action"),
    [
        (0, 3, FailedItemAction.REOPEN_PURGE),
        (2, 3, FailedItemAction.REOPEN_PURGE),
        (3, 3, FailedItemAction.KEEP_FAILED_PURGE),
        (7, 3, FailedItemAction.KEEP_FAILED_PURGE),
        (0, 0, FailedItemAction.KEEP_FAILED_PURGE),
        (50, None, FailedItemAction.REOPEN_PURGE),
    ],
)
def test_a_failed_purge_is_reopened_a_bounded_number_of_times(
    reopen_count: int, max_reopenings: int | None, action: FailedItemAction
) -> None:
    decided = decide_failed_item(
        "purge_document",
        document_status=W,
        purged=False,
        reopen_count=reopen_count,
        max_reopenings=max_reopenings,
    )

    assert decided is action


def test_the_bound_never_applies_to_a_purge_that_has_nothing_left_to_do() -> None:
    assert (
        decide_failed_item(
            "purge_document",
            document_status=W,
            purged=True,
            reopen_count=9,
            max_reopenings=3,
        )
        is FailedItemAction.IGNORE
    )


@pytest.mark.parametrize(
    ("kind", "facts"),
    [
        ("extract_version", {"version_status": VersionStatus.RECEIVED}),
        ("index_version", {"indexation_status": IndexationStatus.PENDING}),
        ("purge_document", {"document_status": W, "purged": False}),
    ],
)
def test_a_failed_item_whose_work_is_live_again_is_left_to_that_work(
    kind: str, facts: dict[str, object]
) -> None:
    assert (
        decide_failed_item(kind, live_work=True, **facts)  # type: ignore[arg-type]
        is FailedItemAction.IGNORE
    )


P1, P2, P3 = UUID(int=1), UUID(int=2), UUID(int=3)


def test_points_beyond_the_manifest_are_those_it_does_not_list() -> None:
    expected = IndexManifest.of([(P1, "a" * 64), (P2, "b" * 64)])

    assert points_beyond_manifest(expected, {P1, P2, P3}) == frozenset({P3})
    assert points_beyond_manifest(expected, {P1}) == frozenset()


@pytest.mark.parametrize(
    ("code", "replayable"),
    [
        ("BLOB_MISSING", True),
        ("EMPTY_DOCUMENT", False),
        ("CODE_D_UNE_VERSION_FUTURE", False),
        (None, False),
    ],
)
def test_a_code_missing_or_unknown_to_the_catalog_is_not_replayable(
    code: str | None, replayable: bool
) -> None:
    assert is_known_replayable(code) is replayable
