"""``KnowledgeUnitSegmenter`` pour ``COURSE``, ``TECHNICAL`` et
``REVISION_SHEET`` (ADR 0009 §1) ; mêmes conventions que ``test_segmenter_qa``.

Règles figées :

- Sections : un titre ouvre une section (chemin = pile des titres, le sien
  compris) ; le texte avant le premier titre est la section racine, au chemin
  vide. Le titre propre d'une section n'est pas dans ses blocs (il est dans le
  chemin) ; ceux de ses sous-sections le sont.
- ``COURSE`` / ``TECHNICAL`` : descente depuis les titres de premier niveau. Si
  tout le contenu d'une section (sous-sections comprises) tient dans
  ``policy.max_tokens`` (comptés sur les blocs rendus, hors en-tête), la
  section est UNE unité ``SECTION`` ; sinon son propre corps (s'il existe) est
  une unité, puis la descente continue dans ses sous-sections. La racine ne
  fusionne jamais : son corps est une unité, ses titres sont traités à part.
  Une section sans corps ne produit aucune unité.
- ``REVISION_SHEET`` : la section la plus haute qui porte du corps est UNE
  unité ``DEFINITION`` avec toutes ses sous-sections, quel qu'en soit le
  volume (le planificateur découpera).
- Aucun contenu n'est perdu : chaque bloc non titre du document est dans
  exactement une unité.
"""

from __future__ import annotations

import pytest

from knowledge_base.domain.value_objects.document_kind import DocumentKind
from knowledge_base.domain.value_objects.knowledge_unit import UnitType
from knowledge_base.domain.value_objects.page_span import PageSpan
from knowledge_base.domain.value_objects.section_path import SectionPath
from knowledge_base.domain.value_objects.structured_document import (
    Heading,
    StructuredDocument,
)
from tests.unit.knowledge_base.domain.services.support import (
    doc,
    h,
    p,
    segment,
    words,
)

SECTION_KINDS = [DocumentKind.COURSE, DocumentKind.TECHNICAL]


def course() -> StructuredDocument:
    """Quatre sections feuilles de 20 mots sous un titre, dont deux imbriquées."""
    return doc(
        h(1, "Cours asyncio"),
        h(2, "Boucle"),
        p(words(20, "b")),
        h(2, "Tâches"),
        h(3, "Création"),
        p(words(20, "c")),
        h(3, "Annulation"),
        p(words(20, "a")),
    )


def paths(units: tuple[object, ...]) -> list[str]:
    return [str(u.section_path) for u in units]  # type: ignore[attr-defined]


@pytest.mark.parametrize("kind", SECTION_KINDS)
def test_a_course_that_fits_the_budget_is_one_section_unit(kind: DocumentKind) -> None:
    (unit,) = segment(course(), kind, max_tokens=1000)

    assert unit.unit_type is UnitType.SECTION
    assert unit.question_text is None
    assert unit.section_path == SectionPath(("Cours asyncio",))
    assert h(2, "Boucle") in unit.blocks
    assert h(3, "Annulation") in unit.blocks
    assert h(1, "Cours asyncio") not in unit.blocks


@pytest.mark.parametrize("kind", SECTION_KINDS)
def test_sub_sections_are_merged_only_while_the_budget_allows(
    kind: DocumentKind,
) -> None:
    # "Boucle" (20 mots) tient ; "Tâches" entière (44 : 2 x 20 + 2 sous-titres de
    # 2 mots) dépasse 40 ; le tout (68) aussi.
    units = segment(course(), kind, max_tokens=40)

    assert paths(units) == [
        "Cours asyncio > Boucle",
        "Cours asyncio > Tâches > Création",
        "Cours asyncio > Tâches > Annulation",
    ]
    assert [u.ordinal for u in units] == [0, 1, 2]


def test_a_section_that_fits_with_its_sub_sections_absorbs_them() -> None:
    # Tâches = 44 mots <= 60 ; le tout (68) dépasse 60.
    units = segment(course(), DocumentKind.COURSE, max_tokens=60)

    assert paths(units)[-1] == "Cours asyncio > Tâches"
    assert h(3, "Création") in units[-1].blocks
    assert h(3, "Annulation") in units[-1].blocks


def test_a_section_exactly_at_the_budget_is_merged_and_one_token_less_is_not() -> None:
    # "Tâches" entière = 44 mots.
    at_budget = segment(course(), DocumentKind.COURSE, max_tokens=44)
    below = segment(course(), DocumentKind.COURSE, max_tokens=43)

    assert paths(at_budget) == ["Cours asyncio > Boucle", "Cours asyncio > Tâches"]
    assert paths(below) == [
        "Cours asyncio > Boucle",
        "Cours asyncio > Tâches > Création",
        "Cours asyncio > Tâches > Annulation",
    ]


def test_a_section_with_an_own_body_and_oversized_children_yields_both() -> None:
    document = doc(
        h(1, "Cours"),
        p(words(10, "i")),
        h(2, "A"),
        p(words(30, "a")),
        h(2, "B"),
        p(words(30, "b")),
    )

    units = segment(document, DocumentKind.COURSE, max_tokens=35)

    assert paths(units) == ["Cours", "Cours > A", "Cours > B"]
    assert units[0].blocks == (p(words(10, "i")),)


def test_text_before_the_first_heading_is_a_root_unit_with_an_empty_path() -> None:
    document = doc(p("Préambule."), h(1, "Cours"), p("Contenu."))

    units = segment(document, DocumentKind.COURSE)

    assert paths(units) == ["", "Cours"]
    assert units[0].blocks == (p("Préambule."),)


def test_a_document_without_headings_is_one_unit_at_the_root() -> None:
    document = doc(p(words(5, "x")), p(words(5, "y")))

    (unit,) = segment(document, DocumentKind.TECHNICAL, max_tokens=3)

    assert unit.section_path == SectionPath(())
    assert len(unit.blocks) == 2


def test_a_section_with_only_sub_headings_yields_no_unit_of_its_own() -> None:
    document = doc(h(1, "Cours"), h(2, "A"), p(words(30, "a")), h(2, "B"), p("b"))

    units = segment(document, DocumentKind.COURSE, max_tokens=31)

    assert paths(units) == ["Cours > A", "Cours > B"]


def test_a_course_does_not_treat_question_headings_as_qa() -> None:
    document = doc(h(1, "Cours"), h(2, "Qu'est-ce que Depends ?"), p("Injection."))

    units = segment(document, DocumentKind.COURSE)

    assert {u.unit_type for u in units} == {UnitType.SECTION}


def test_pages_of_a_section_unit_are_the_pages_of_its_blocks() -> None:
    document = doc(h(1, "Cours", page=4), p("a", page=4), p("b", page=6))

    (unit,) = segment(document, DocumentKind.COURSE)

    assert unit.page_span == PageSpan(4, 6)


def test_no_non_heading_block_is_lost_or_duplicated() -> None:
    document = course()

    for budget in (1000, 60, 40, 25, 5):
        units = segment(document, DocumentKind.COURSE, max_tokens=budget)
        kept = [b for u in units for b in u.blocks if not isinstance(b, Heading)]
        expected = [b for b in document.blocks if not isinstance(b, Heading)]
        assert kept == expected, f"budget {budget}"


def revision_sheet() -> StructuredDocument:
    return doc(
        h(1, "Fiche de révision Python"),
        h(2, "GIL"),
        p("Verrou global de l'interpréteur CPython."),
        h(2, "Décorateur"),
        p("Fonction qui enveloppe une fonction."),
        h(3, "Exemple"),
        p("@cache"),
    )


def test_a_revision_sheet_yields_one_definition_per_top_section_with_a_body() -> None:
    units = segment(revision_sheet(), DocumentKind.REVISION_SHEET)

    assert [u.unit_type for u in units] == [UnitType.DEFINITION, UnitType.DEFINITION]
    assert paths(units) == [
        "Fiche de révision Python > GIL",
        "Fiche de révision Python > Décorateur",
    ]
    assert all(u.question_text is None for u in units)


def test_a_definition_keeps_its_sub_sections_whatever_the_budget() -> None:
    units = segment(revision_sheet(), DocumentKind.REVISION_SHEET, max_tokens=2)

    assert units[1].blocks == (
        p("Fonction qui enveloppe une fonction."),
        h(3, "Exemple"),
        p("@cache"),
    )
