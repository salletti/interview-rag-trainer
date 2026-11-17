"""Retrait d'un document (knowledge-base.md §5.1, §10).

API figée :

- ``document.withdraw()`` -> ``None`` : ``status = WITHDRAWN``,
  ``visibility_revision + 1``, états des versions et ``published_version_id``
  inchangés ; événement ``DocumentWithdrawn(document_id, visibility_revision)``.
- Retirer un document déjà ``WITHDRAWN`` lève ``DocumentWithdrawnError`` (pas
  d'idempotence silencieuse : la révision ne doit pas être incrémentée deux
  fois).
- Ensuite ``import_version`` lève ``DocumentWithdrawnError`` (aucune version
  ajoutée, aucun événement).
"""

from __future__ import annotations

import pytest

from knowledge_base.domain.entities.knowledge_document import (
    DocumentStatus,
    VersionStatus,
)
from knowledge_base.domain.events.document_events import DocumentWithdrawn
from knowledge_base.domain.exceptions.document_errors import DocumentWithdrawnError
from tests.unit.knowledge_base.domain.builders import (
    DOCUMENT_ID,
    import_next,
    new_document,
    publish_numbered,
    source,
    version_id,
)


def test_withdraw_marks_the_document_withdrawn_and_bumps_visibility_revision() -> None:
    document = new_document()
    publish_numbered(document, 1)

    document.withdraw()

    assert document.status is DocumentStatus.WITHDRAWN
    assert document.visibility_revision == 2


def test_withdraw_signals_the_new_revision() -> None:
    document = new_document()
    publish_numbered(document, 1)
    document.pull_events()

    document.withdraw()

    assert document.pull_events() == [
        DocumentWithdrawn(document_id=DOCUMENT_ID, visibility_revision=2)
    ]


def test_withdraw_does_not_change_version_states() -> None:
    document = new_document()
    publish_numbered(document, 1)
    import_next(document, 2)
    before = document.versions

    document.withdraw()

    assert document.versions == before
    assert [v.status for v in document.versions] == [
        VersionStatus.PUBLISHED,
        VersionStatus.RECEIVED,
    ]
    assert document.published_version_id == version_id(1)


def test_a_withdrawn_document_accepts_no_new_version() -> None:
    document = new_document()
    document.withdraw()
    document.pull_events()

    with pytest.raises(DocumentWithdrawnError):
        document.import_version(version_id=version_id(2), source=source(2))

    assert [v.number for v in document.versions] == [1]
    assert document.pull_events() == []


def test_a_withdrawn_document_does_not_discard_anything_on_refused_import() -> None:
    document = new_document()
    document.withdraw()

    with pytest.raises(DocumentWithdrawnError):
        import_next(document, 2)

    assert document.versions[0].status is VersionStatus.RECEIVED


def test_withdrawing_twice_is_refused_and_does_not_bump_the_revision() -> None:
    document = new_document()
    document.withdraw()
    document.pull_events()

    with pytest.raises(DocumentWithdrawnError):
        document.withdraw()

    assert document.visibility_revision == 1
    assert document.pull_events() == []
