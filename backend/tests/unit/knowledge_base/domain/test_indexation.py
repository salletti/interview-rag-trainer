"""Agrégat ``Indexation`` : transitions du diagramme §5.3 (S4 ; knowledge-base.md
§3.3, §4.1 I8).

API figée (``knowledge_base.domain.entities.indexation``) :

- ``IndexationStatus`` : ``PENDING``, ``COMPLETED``, ``FAILED``, ``CANCELLED``.
- ``Indexation.pending(*, version_id, index_id, chunking_policy_version)`` ;
  ``Indexation.restore(*, version_id, index_id, status, chunking_policy_version,
  manifest_digest, point_count, completed_at, failure)`` (persistance, sans
  contrôle). Lecture : ``version_id, index_id, status, chunking_policy_version,
  manifest_digest, point_count, completed_at`` (``None`` hors ``COMPLETED``),
  ``failure: FailureReason | None`` (renseignée seulement à ``FAILED``).
- ``complete(*, manifest_digest, point_count, completed_at)`` : ``PENDING`` ->
  ``COMPLETED`` ; digest non blanc, ``point_count >= 0``, sinon ``ValueError``.
- ``fail(reason)`` : ``PENDING`` -> ``FAILED`` ; ``retry()`` : ``FAILED`` ->
  ``PENDING`` (efface la cause) ; ``cancel()`` : ``PENDING`` ou ``FAILED`` ->
  ``CANCELLED``.
- Toute autre transition lève ``InvalidIndexationTransitionError``
  (``knowledge_base.domain.exceptions.indexation_errors``) sans aucun effet.
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
DONE_AT = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)
DIGEST = "ab" * 32
REASON = FailureReason(code="COLLECTION_MISMATCH", message="dimension", retryable=True)


def pending() -> Indexation:
    return Indexation.pending(
        version_id=VERSION_ID, index_id=INDEX_ID, chunking_policy_version="chunking-1"
    )


def completed() -> Indexation:
    indexation = pending()
    indexation.complete(manifest_digest=DIGEST, point_count=3, completed_at=DONE_AT)
    return indexation


def failed() -> Indexation:
    indexation = pending()
    indexation.fail(REASON)
    return indexation


def cancelled() -> Indexation:
    indexation = pending()
    indexation.cancel()
    return indexation


def test_a_new_indexation_is_pending_and_empty() -> None:
    indexation = pending()

    assert (indexation.version_id, indexation.index_id) == (VERSION_ID, INDEX_ID)
    assert indexation.status is IndexationStatus.PENDING
    assert indexation.chunking_policy_version == "chunking-1"
    assert indexation.manifest_digest is None
    assert indexation.point_count is None
    assert indexation.completed_at is None
    assert indexation.failure is None


def test_completing_records_the_manifest_digest_count_and_date() -> None:
    indexation = completed()

    assert indexation.status is IndexationStatus.COMPLETED
    assert indexation.manifest_digest == DIGEST
    assert indexation.point_count == 3
    assert indexation.completed_at == DONE_AT


def test_completing_an_empty_version_is_allowed() -> None:
    indexation = pending()

    indexation.complete(manifest_digest=DIGEST, point_count=0, completed_at=DONE_AT)

    assert indexation.status is IndexationStatus.COMPLETED
    assert indexation.point_count == 0


@pytest.mark.parametrize(("digest", "count"), [("", 1), ("  ", 1), (DIGEST, -1)])
def test_an_incoherent_completion_is_rejected_without_effect(
    digest: str, count: int
) -> None:
    indexation = pending()

    with pytest.raises(ValueError, match=r"manifest|point"):
        indexation.complete(
            manifest_digest=digest, point_count=count, completed_at=DONE_AT
        )

    assert indexation.status is IndexationStatus.PENDING
    assert indexation.manifest_digest is None


def test_failing_keeps_the_reason() -> None:
    indexation = failed()

    assert indexation.status is IndexationStatus.FAILED
    assert indexation.failure == REASON


def test_retrying_a_failed_indexation_reopens_it_and_clears_the_reason() -> None:
    indexation = failed()

    indexation.retry()

    assert indexation.status is IndexationStatus.PENDING
    assert indexation.failure is None


@pytest.mark.parametrize("make", [pending, failed])
def test_a_pending_or_failed_indexation_can_be_cancelled(
    make: Callable[[], Indexation],
) -> None:
    indexation = make()

    indexation.cancel()

    assert indexation.status is IndexationStatus.CANCELLED


def test_a_cancelled_indexation_drops_the_failure_reason() -> None:
    indexation = failed()

    indexation.cancel()

    assert indexation.failure is None


@pytest.mark.parametrize(
    ("make", "operation"),
    [
        (completed, "complete"),
        (completed, "fail"),
        (completed, "retry"),
        (completed, "cancel"),
        (failed, "complete"),
        (failed, "fail"),
        (cancelled, "complete"),
        (cancelled, "fail"),
        (cancelled, "retry"),
        (cancelled, "cancel"),
        (pending, "retry"),
    ],
)
def test_transitions_absent_from_the_diagram_are_refused_without_effect(
    make: Callable[[], Indexation], operation: str
) -> None:
    indexation = make()
    before = (
        indexation.status,
        indexation.manifest_digest,
        indexation.point_count,
        indexation.completed_at,
        indexation.failure,
    )
    attempts = {
        "complete": lambda: indexation.complete(
            manifest_digest="cd" * 32, point_count=9, completed_at=DONE_AT
        ),
        "fail": lambda: indexation.fail(REASON),
        "retry": indexation.retry,
        "cancel": indexation.cancel,
    }

    with pytest.raises(InvalidIndexationTransitionError):
        attempts[operation]()

    assert before == (
        indexation.status,
        indexation.manifest_digest,
        indexation.point_count,
        indexation.completed_at,
        indexation.failure,
    )


def test_a_restored_indexation_is_taken_as_is() -> None:
    indexation = Indexation.restore(
        version_id=VERSION_ID,
        index_id=INDEX_ID,
        status=IndexationStatus.COMPLETED,
        chunking_policy_version="chunking-1",
        manifest_digest=DIGEST,
        point_count=5,
        completed_at=DONE_AT,
        failure=None,
    )

    assert indexation.status is IndexationStatus.COMPLETED
    assert indexation.point_count == 5
