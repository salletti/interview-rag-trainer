"""Transitions de ``DocumentVersion`` (knowledge-base.md §5.2).

API figée :

- ``record_extraction(version_id)`` : ``RECEIVED`` -> ``EXTRACTED`` ; événement
  ``DocumentVersionExtracted(document_id, version_id)``.
- ``fail_extraction(version_id, reason: FailureReason)`` : ``RECEIVED`` ->
  ``FAILED``, ``version.failure = reason`` (pas d'événement en S1).
- ``retry_extraction(version_id)`` : ``FAILED`` -> ``RECEIVED``,
  ``failure`` remis à ``None`` ; refusé si la version est ``DISCARDED``.
- Toute transition hors du diagramme : ``InvalidVersionTransitionError`` sans
  effet ; id inconnu : ``VersionNotFoundError``. ``DISCARDED`` et
  ``SUPERSEDED`` sont terminaux. ``SUPERSEDED`` n'est atteint que depuis
  ``PUBLISHED``, par la publication d'une version plus récente.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from knowledge_base.domain.entities.knowledge_document import (
    KnowledgeDocument,
    VersionStatus,
)
from knowledge_base.domain.events.document_events import DocumentVersionExtracted
from knowledge_base.domain.exceptions.document_errors import (
    InvalidVersionTransitionError,
    VersionNotFoundError,
)
from tests.unit.knowledge_base.domain.builders import (
    DOCUMENT_ID,
    extract,
    failure,
    import_next,
    new_document,
    publish_numbered,
    restored_document,
    version_id,
)

ALL = list(VersionStatus)
V1 = version_id(1)


def status_of(document: KnowledgeDocument) -> VersionStatus:
    return document.get_version(V1).status


def test_record_extraction_moves_received_to_extracted() -> None:
    document = new_document()

    document.record_extraction(V1)

    assert status_of(document) is VersionStatus.EXTRACTED
    assert document.pull_events() == [
        DocumentVersionExtracted(document_id=DOCUMENT_ID, version_id=V1)
    ]


def test_fail_extraction_moves_received_to_failed_and_keeps_the_reason() -> None:
    document = new_document()

    document.fail_extraction(V1, failure())

    version = document.get_version(V1)
    assert version.status is VersionStatus.FAILED
    assert version.failure == failure()


def test_retry_moves_failed_back_to_received_and_clears_the_failure() -> None:
    document = new_document()
    document.fail_extraction(V1, failure())

    document.retry_extraction(V1)

    version = document.get_version(V1)
    assert version.status is VersionStatus.RECEIVED
    assert version.failure is None


def test_a_retried_version_can_then_be_extracted() -> None:
    document = new_document()
    document.fail_extraction(V1, failure())
    document.retry_extraction(V1)

    document.record_extraction(V1)

    assert status_of(document) is VersionStatus.EXTRACTED


def test_retry_is_refused_for_a_version_discarded_in_the_meantime() -> None:
    document = new_document()
    document.fail_extraction(V1, failure())
    import_next(document, 2)  # v1 FAILED -> DISCARDED (I6)
    assert status_of(document) is VersionStatus.DISCARDED

    with pytest.raises(InvalidVersionTransitionError):
        document.retry_extraction(V1)

    assert status_of(document) is VersionStatus.DISCARDED


@pytest.mark.parametrize("status", [s for s in ALL if s is not VersionStatus.RECEIVED])
def test_record_extraction_is_only_allowed_from_received(
    status: VersionStatus,
) -> None:
    document = restored_document(
        [(1, status)], published=1 if status.name == "PUBLISHED" else None
    )

    with pytest.raises(InvalidVersionTransitionError):
        document.record_extraction(V1)

    assert status_of(document) is status
    assert document.pull_events() == []


@pytest.mark.parametrize("status", [s for s in ALL if s is not VersionStatus.RECEIVED])
def test_fail_extraction_is_only_allowed_from_received(status: VersionStatus) -> None:
    document = restored_document(
        [(1, status)], published=1 if status.name == "PUBLISHED" else None
    )
    before = document.get_version(V1)

    with pytest.raises(InvalidVersionTransitionError):
        document.fail_extraction(V1, failure())

    assert document.get_version(V1) == before


@pytest.mark.parametrize("status", [s for s in ALL if s is not VersionStatus.FAILED])
def test_retry_is_only_allowed_from_failed(status: VersionStatus) -> None:
    document = restored_document(
        [(1, status)], published=1 if status.name == "PUBLISHED" else None
    )

    with pytest.raises(InvalidVersionTransitionError):
        document.retry_extraction(V1)

    assert status_of(document) is status


@pytest.mark.parametrize("status", [VersionStatus.DISCARDED, VersionStatus.SUPERSEDED])
def test_terminal_states_accept_no_transition(status: VersionStatus) -> None:
    document = restored_document([(1, status)])

    actions: tuple[Callable[[], None], ...] = (
        lambda: document.record_extraction(V1),
        lambda: document.fail_extraction(V1, failure()),
        lambda: document.retry_extraction(V1),
        lambda: document.publish(V1),
    )
    for action in actions:
        with pytest.raises(InvalidVersionTransitionError):
            action()

    assert status_of(document) is status


def test_superseded_is_only_reached_from_published_by_a_newer_publication() -> None:
    document = new_document()
    publish_numbered(document, 1)
    import_next(document, 2)
    document.record_extraction(version_id(2))
    assert (
        status_of(document) is VersionStatus.PUBLISHED
    )  # import et extraction n'y touchent pas

    document.publish(version_id(2))

    assert status_of(document) is VersionStatus.SUPERSEDED


def test_an_unpublished_version_is_never_superseded() -> None:
    document = new_document()
    extract(document, 1)  # EXTRACTED, jamais publiée
    import_next(document, 2)

    assert status_of(document) is VersionStatus.DISCARDED


@pytest.mark.parametrize(
    "action",
    [
        lambda d: d.record_extraction(version_id(99)),
        lambda d: d.fail_extraction(version_id(99), failure()),
        lambda d: d.retry_extraction(version_id(99)),
    ],
    ids=["record_extraction", "fail_extraction", "retry_extraction"],
)
def test_transitions_on_an_unknown_version_are_refused(action: object) -> None:
    document = new_document()

    with pytest.raises(VersionNotFoundError):
        action(document)  # type: ignore[operator]
