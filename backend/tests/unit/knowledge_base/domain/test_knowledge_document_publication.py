"""Publication monotone : I4, I5 (knowledge-base.md §4.1, §8, §10).

API figée :

- ``document.publish(version_id)`` -> ``None`` ; effets : la version devient
  ``PUBLISHED``, la précédente ``SUPERSEDED``, ``published_version_id`` et
  ``visibility_revision + 1``. Événement ``DocumentVersionPublished(document_id,
  version_id, number, visibility_revision, superseded_version_id | None)``.
- Refus, sans effet de bord ni événement : ``DocumentWithdrawnError`` (document
  ``WITHDRAWN``, testé en premier), ``VersionNotFoundError`` (id inconnu),
  ``InvalidVersionTransitionError`` (version non ``EXTRACTED``),
  ``StalePublicationError`` (``number <= `` numéro de la version publiée).
- Garde-fous I5 : l'agrégat écarte déjà les anciennes versions à l'import (I6),
  donc l'état « EXTRACTED d'un numéro inférieur au publié » n'est atteint que
  par ``restore`` ; le test prouve que la règle tient indépendamment d'I6 (§8.3).
"""

from __future__ import annotations

import pytest

from knowledge_base.domain.entities.knowledge_document import (
    DocumentStatus,
    VersionStatus,
)
from knowledge_base.domain.events.document_events import DocumentVersionPublished
from knowledge_base.domain.exceptions.document_errors import (
    DocumentWithdrawnError,
    InvalidVersionTransitionError,
    KnowledgeDocumentError,
    StalePublicationError,
    VersionNotFoundError,
)
from tests.unit.knowledge_base.domain.builders import (
    DOCUMENT_ID,
    extract,
    import_next,
    new_document,
    publish_numbered,
    restored_document,
    version_id,
)


def test_publishing_an_extracted_version_makes_it_the_published_one() -> None:
    document = new_document()
    extract(document, 1)

    document.publish(version_id(1))

    assert document.get_version(version_id(1)).status is VersionStatus.PUBLISHED
    assert document.published_version_id == version_id(1)
    assert document.visibility_revision == 1


def test_publishing_n_supersedes_the_previous_published_version() -> None:
    document = new_document()
    publish_numbered(document, 1)
    import_next(document, 2)

    publish_numbered(document, 2)

    assert document.get_version(version_id(1)).status is VersionStatus.SUPERSEDED
    assert document.get_version(version_id(2)).status is VersionStatus.PUBLISHED
    assert document.published_version_id == version_id(2)


def test_at_most_one_version_is_published() -> None:
    document = new_document()
    for number in (1, 2, 3):
        if number > 1:
            import_next(document, number)
        publish_numbered(document, number)

        published = [
            v for v in document.versions if v.status is VersionStatus.PUBLISHED
        ]
        assert [v.number for v in published] == [number]


def test_each_publication_increments_visibility_revision() -> None:
    document = new_document()
    publish_numbered(document, 1)
    import_next(document, 2)
    publish_numbered(document, 2)

    assert document.visibility_revision == 2


def test_importing_does_not_change_visibility_revision() -> None:
    document = new_document()
    publish_numbered(document, 1)

    import_next(document, 2)

    assert document.visibility_revision == 1


def test_publication_is_signalled_with_the_superseded_version() -> None:
    document = new_document()
    publish_numbered(document, 1)
    import_next(document, 2)
    extract(document, 2)
    document.pull_events()

    document.publish(version_id(2))

    assert document.pull_events() == [
        DocumentVersionPublished(
            document_id=DOCUMENT_ID,
            version_id=version_id(2),
            number=2,
            visibility_revision=2,
            superseded_version_id=version_id(1),
        )
    ]


def test_first_publication_has_no_superseded_version() -> None:
    document = new_document()
    extract(document, 1)
    document.pull_events()

    document.publish(version_id(1))

    (event,) = document.pull_events()
    assert isinstance(event, DocumentVersionPublished)
    assert event.superseded_version_id is None
    assert event.visibility_revision == 1


# --- I5 : refus ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        VersionStatus.RECEIVED,
        VersionStatus.FAILED,
        VersionStatus.DISCARDED,
        VersionStatus.SUPERSEDED,
        VersionStatus.PUBLISHED,
    ],
)
def test_publication_is_refused_unless_the_version_is_extracted(
    status: VersionStatus,
) -> None:
    document = restored_document([(1, status)])

    with pytest.raises(InvalidVersionTransitionError):
        document.publish(version_id(1))

    assert document.get_version(version_id(1)).status is status
    assert document.visibility_revision == 0
    assert document.pull_events() == []


def test_publication_is_refused_if_number_is_not_above_the_published_one() -> None:
    # v2 est publiée ; v1 est EXTRACTED : seule la règle monotone l'arrête.
    document = restored_document(
        [(1, VersionStatus.EXTRACTED), (2, VersionStatus.PUBLISHED)],
        published=2,
        visibility_revision=1,
    )

    with pytest.raises(StalePublicationError):
        document.publish(version_id(1))

    assert document.get_version(version_id(1)).status is VersionStatus.EXTRACTED
    assert document.get_version(version_id(2)).status is VersionStatus.PUBLISHED
    assert document.published_version_id == version_id(2)
    assert document.visibility_revision == 1
    assert document.pull_events() == []


def test_publication_is_refused_on_a_withdrawn_document() -> None:
    document = restored_document(
        [(1, VersionStatus.EXTRACTED)], status=DocumentStatus.WITHDRAWN
    )

    with pytest.raises(DocumentWithdrawnError):
        document.publish(version_id(1))

    assert document.get_version(version_id(1)).status is VersionStatus.EXTRACTED
    assert document.published_version_id is None
    assert document.visibility_revision == 0
    assert document.pull_events() == []


def test_publishing_an_unknown_version_is_refused() -> None:
    document = new_document()

    with pytest.raises(VersionNotFoundError):
        document.publish(version_id(99))


def test_publication_errors_share_the_domain_base() -> None:
    for error in (
        DocumentWithdrawnError,
        InvalidVersionTransitionError,
        StalePublicationError,
        VersionNotFoundError,
    ):
        assert issubclass(error, KnowledgeDocumentError)
