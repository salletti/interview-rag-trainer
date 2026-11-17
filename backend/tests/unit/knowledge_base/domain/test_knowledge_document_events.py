"""Collecte des Domain Events : ``pull_events()`` rend la liste puis la vide."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from knowledge_base.domain.entities.knowledge_document import KnowledgeDocument
from knowledge_base.domain.events.document_events import (
    DocumentImported,
    DocumentVersionExtracted,
    DocumentWithdrawn,
)
from knowledge_base.domain.value_objects.document_kind import DocumentKind
from tests.unit.knowledge_base.domain.builders import (
    DOCUMENT_ID,
    content_hash,
    extract,
    import_next,
    version_id,
)
from tests.unit.knowledge_base.domain.builders import source as make_source


def fresh() -> KnowledgeDocument:
    return KnowledgeDocument.import_new(
        document_id=DOCUMENT_ID,
        version_id=version_id(1),
        title="Python avancé",
        kind=DocumentKind.COURSE,
        topics=(),
        language="fr",
        source=make_source(1),
    )


def test_events_accumulate_in_order_until_pulled() -> None:
    document = fresh()
    extract(document, 1)
    document.withdraw()

    assert [type(e) for e in document.pull_events()] == [
        DocumentImported,
        DocumentVersionExtracted,
        DocumentWithdrawn,
    ]


def test_pull_events_empties_the_buffer() -> None:
    document = fresh()

    first = document.pull_events()
    second = document.pull_events()

    assert len(first) == 1
    assert second == []


def test_events_raised_after_a_pull_are_returned_by_the_next_pull_only() -> None:
    document = fresh()
    document.pull_events()

    extract(document, 1)

    assert [type(e) for e in document.pull_events()] == [DocumentVersionExtracted]
    assert document.pull_events() == []


def test_the_returned_list_is_a_copy_the_caller_may_keep() -> None:
    document = fresh()
    pulled = document.pull_events()

    import_next(document, 2)

    assert [type(e) for e in pulled] == [DocumentImported]
    assert pulled[0] == DocumentImported(
        document_id=DOCUMENT_ID,
        version_id=version_id(1),
        number=1,
        content_hash=content_hash(1),
    )


def test_events_are_immutable() -> None:
    event = fresh().pull_events()[0]

    with pytest.raises(FrozenInstanceError):
        event.number = 2  # type: ignore[attr-defined]
