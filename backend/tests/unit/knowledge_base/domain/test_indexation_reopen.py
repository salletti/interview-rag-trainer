"""Ré-ouverture d'une indexation ``COMPLETED`` dont le manifest ne se vérifie
plus (S8 ; knowledge-base.md §9 : « la réconciliation recalcule le manifest des
indexations COMPLETED par échantillonnage et ré-ouvre l'indexation en cas
d'écart »).

API figée (``knowledge_base.domain.entities.indexation.Indexation``) :

- ``reopen()`` : ``COMPLETED`` -> ``PENDING``. Efface le manifest, le nombre de
  points et la date de complétion (I8 : ``COMPLETED`` si et seulement si le
  manifest vérifié est enregistré, contrainte
  ``manifest_only_when_completed``). Toute autre origine lève
  ``InvalidIndexationTransitionError`` sans effet.
- Ce n'est PAS ``retry()`` (``FAILED`` -> ``PENDING``) : le diagramme §5.3 est
  complété d'une arête ``COMPLETED -> PENDING`` réservée à la réconciliation.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

import pytest

from knowledge_base.domain.entities.indexation import Indexation, IndexationStatus
from knowledge_base.domain.exceptions.indexation_errors import (
    InvalidIndexationTransitionError,
)
from knowledge_base.domain.value_objects.failure_reason import FailureReason

VERSION_ID, INDEX_ID = UUID(int=1), UUID(int=2)
REASON = FailureReason(code="COLLECTION_MISMATCH", message="dimension", retryable=True)


def pending() -> Indexation:
    return Indexation.pending(
        version_id=VERSION_ID, index_id=INDEX_ID, chunking_policy_version="chunking-1"
    )


def completed() -> Indexation:
    indexation = pending()
    indexation.complete(
        manifest_digest="ab" * 32,
        point_count=3,
        completed_at=datetime(2026, 11, 18, 10, 0, tzinfo=UTC),
    )
    return indexation


def test_reopening_a_completed_indexation_makes_it_pending_and_empty() -> None:
    indexation = completed()

    indexation.reopen()

    assert indexation.status is IndexationStatus.PENDING
    assert indexation.manifest_digest is None
    assert indexation.point_count is None
    assert indexation.completed_at is None
    assert indexation.failure is None
    assert indexation.chunking_policy_version == "chunking-1"


def test_a_reopened_indexation_can_be_completed_again() -> None:
    indexation = completed()
    indexation.reopen()

    indexation.complete(
        manifest_digest="cd" * 32,
        point_count=4,
        completed_at=datetime(2026, 11, 19, 10, 0, tzinfo=UTC),
    )

    assert indexation.status is IndexationStatus.COMPLETED
    assert indexation.manifest_digest == "cd" * 32


def failed() -> Indexation:
    indexation = pending()
    indexation.fail(REASON)
    return indexation


def cancelled() -> Indexation:
    indexation = pending()
    indexation.cancel()
    return indexation


@pytest.mark.parametrize("make", [pending, failed, cancelled])
def test_only_a_completed_indexation_can_be_reopened(
    make: Callable[[], Indexation],
) -> None:
    indexation = make()
    before = indexation.status

    with pytest.raises(InvalidIndexationTransitionError):
        indexation.reopen()

    assert indexation.status is before
