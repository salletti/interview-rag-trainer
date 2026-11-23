"""Identifiants déterministes des unités (knowledge-base.md §3.4).

``unit_id`` est un UUIDv5 dérivé de (``version_id``, ``policy.version``,
``ordinal``) : deux exécutions donnent les mêmes identifiants, une autre
version, une autre politique ou un autre rang en donnent d'autres.
"""

from __future__ import annotations

from uuid import UUID

from knowledge_base.domain.value_objects.document_kind import DocumentKind
from tests.unit.knowledge_base.domain.services.support import doc, h, p, segment

DOCUMENT = doc(
    h(1, "Fiche"),
    h(2, "Question : Un ?"),
    p("a"),
    h(2, "Question : Deux ?"),
    p("b"),
    h(2, "Question : Trois ?"),
    p("c"),
)
QA = DocumentKind.QA_SHEET


def ids(**kwargs: object) -> list[UUID]:
    units = segment(DOCUMENT, QA, **kwargs)  # type: ignore[arg-type]
    return [u.unit_id for u in units]


def test_two_runs_give_the_same_unit_identifiers() -> None:
    assert ids() == ids()
    assert segment(DOCUMENT, QA) == segment(DOCUMENT, QA)


def test_unit_identifiers_are_distinct_uuid5() -> None:
    unit_ids = ids()

    assert len(set(unit_ids)) == 3
    assert all(u.version == 5 for u in unit_ids)


def test_identifiers_change_with_the_version_and_the_policy_version() -> None:
    reference = set(ids())

    assert reference.isdisjoint(ids(version_id=UUID(int=99)))
    assert reference.isdisjoint(ids(policy_version="autre-politique"))


def test_a_changed_content_with_the_same_structure_keeps_the_identifiers() -> None:
    changed = doc(
        h(1, "Fiche"),
        h(2, "Question : Un ?"),
        p("a modifié"),
        h(2, "Question : Deux ?"),
        p("b"),
        h(2, "Question : Trois ?"),
        p("c"),
    )

    assert [u.unit_id for u in segment(changed, QA)] == ids()
