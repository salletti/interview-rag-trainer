"""``KnowledgeUnitSegmenter`` pour ``QA_SHEET`` : golden tests sur des
``StructuredDocument`` construits à la main (ADR 0009 §1-2, O13).

API figée (``knowledge_base.domain.services.knowledge_unit_segmenter``) :

- ``KnowledgeUnitSegmenter(token_counter, policy)`` ;
  ``segment(document, *, kind: DocumentKind, version_id: UUID) ->
  Sequence[KnowledgeUnit]``, dans l'ordre du document, ``ordinal`` = 0, 1, 2…
- Question : un titre est une question s'il commence par ``Question``/``Q``
  (casse indifférente, numéro facultatif) suivi de ``:``, ou s'il se termine par
  ``?`` (ou `` ?``). ``question_text`` = le titre sans ce préfixe.
- Convention explicite : un front-matter ``kind: qa_sheet`` (``metadata``) fait
  de **tout titre de niveau 2** une question, quelle que soit sa forme et le
  ``DocumentKind`` déclaré, et l'emporte sur l'heuristique (les titres
  « question » plus profonds sont alors du corps).
- Une unité ``QA`` s'étend de la question (exclue des blocs) jusqu'à la
  question suivante ou un titre de niveau strictement inférieur ; les titres
  « Réponse », « Explication » du corps restent des blocs ``Heading``.
  ``section_path`` = les titres qui contiennent la question, sans elle.
- Sans question détectée, un ``QA_SHEET`` se découpe comme un cours (unités
  ``SECTION``) : aucun contenu n'est perdu.
- Le contenu hors question (introduction) forme des unités ``SECTION``.
- ``page_span`` d'une unité : plus petite et plus grande page de ses blocs.
"""

from __future__ import annotations

import pytest

from knowledge_base.domain.value_objects.document_kind import DocumentKind
from knowledge_base.domain.value_objects.knowledge_unit import UnitType
from knowledge_base.domain.value_objects.page_span import PageSpan
from knowledge_base.domain.value_objects.section_path import SectionPath
from knowledge_base.domain.value_objects.structured_document import (
    CodeBlock,
    Paragraph,
)
from tests.unit.knowledge_base.domain.services.support import (
    COUNTER,
    code,
    doc,
    h,
    p,
    segment,
    words,
)

QA = DocumentKind.QA_SHEET

DEPENDS_CODE = "def endpoint(db: Session = Depends(get_db)):\n    return db.query()"


def test_question_answer_code_and_explanation_make_a_single_unit() -> None:
    document = doc(
        h(1, "Fiche FastAPI"),
        h(2, "Question : Qu'est-ce que Depends ?"),
        h(3, "Réponse"),
        p("Depends déclare une dépendance injectée par FastAPI."),
        code(DEPENDS_CODE),
        h(3, "Explication"),
        p("FastAPI résout get_db à chaque requête."),
    )

    (unit,) = segment(document, QA)

    assert unit.unit_type is UnitType.QA
    assert unit.question_text == "Qu'est-ce que Depends ?"
    assert unit.section_path == SectionPath(("Fiche FastAPI",))
    assert unit.ordinal == 0
    assert unit.blocks == (
        h(3, "Réponse"),
        p("Depends déclare une dépendance injectée par FastAPI."),
        code(DEPENDS_CODE),
        h(3, "Explication"),
        p("FastAPI résout get_db à chaque requête."),
    )


def test_each_question_starts_its_own_unit_in_document_order() -> None:
    document = doc(
        h(1, "Fiche asyncio"),
        h(2, "Question : Qu'est-ce qu'une coroutine ?"),
        p("Une fonction async def."),
        h(2, "Question : Qu'est-ce que la boucle d'événements ?"),
        p("Le planificateur des coroutines."),
    )

    units = segment(document, QA)

    assert [u.ordinal for u in units] == [0, 1]
    assert [u.question_text for u in units] == [
        "Qu'est-ce qu'une coroutine ?",
        "Qu'est-ce que la boucle d'événements ?",
    ]
    assert units[1].blocks == (p("Le planificateur des coroutines."),)


@pytest.mark.parametrize(
    ("heading", "question"),
    [
        ("Question : Qu'est-ce que Depends", "Qu'est-ce que Depends"),
        ("Question: What is a coroutine", "What is a coroutine"),
        ("Q: Pourquoi utiliser async", "Pourquoi utiliser async"),
        ("q : pourquoi utiliser async", "pourquoi utiliser async"),
        ("Question 2 : Qu'est-ce qu'un middleware", "Qu'est-ce qu'un middleware"),
        ("Q2: What is a middleware", "What is a middleware"),
        ("Qu'est-ce que Depends ?", "Qu'est-ce que Depends ?"),
        ("What is dependency injection?", "What is dependency injection?"),
    ],
)
def test_question_headings_are_recognised_without_front_matter(
    heading: str, question: str
) -> None:
    document = doc(h(1, "Fiche"), h(2, heading), p("Réponse."))

    (unit,) = segment(document, QA)

    assert unit.unit_type is UnitType.QA
    assert unit.question_text == question


@pytest.mark.parametrize(
    "heading", ["Introduction", "Questionnaire final", "Quick start", "Les décorateurs"]
)
def test_headings_that_are_not_questions_do_not_start_a_qa_unit(heading: str) -> None:
    document = doc(h(1, "Fiche"), h(2, heading), p("Du texte."))

    units = segment(document, QA)

    assert [u.unit_type for u in units] == [UnitType.SECTION]


def test_a_qa_sheet_without_any_question_falls_back_to_sections() -> None:
    document = doc(
        h(1, "Notes"), h(2, "Depends"), p("Injection."), h(2, "Lifespan"), p("Cycle.")
    )

    units = segment(document, QA, max_tokens=3)

    assert {u.unit_type for u in units} == {UnitType.SECTION}
    kept = [b for u in units for b in u.blocks if isinstance(b, Paragraph)]
    assert kept == [p("Injection."), p("Cycle.")]


def test_front_matter_makes_every_level_2_heading_a_question() -> None:
    document = doc(
        h(1, "Fiche FastAPI"),
        h(2, "Depends"),
        p("Injection de dépendances."),
        h(2, "Lifespan"),
        p("Démarrage et arrêt de l'application."),
        kind="qa_sheet",
    )

    units = segment(document, DocumentKind.COURSE)

    assert [u.unit_type for u in units] == [UnitType.QA, UnitType.QA]
    assert [u.question_text for u in units] == ["Depends", "Lifespan"]


def test_front_matter_takes_priority_over_the_question_heuristic() -> None:
    document = doc(
        h(1, "Fiche"),
        h(2, "Dépendances"),
        h(3, "Question : Qu'est-ce que Depends ?"),
        p("Réponse."),
        kind="qa_sheet",
    )

    (unit,) = segment(document, QA)

    assert unit.question_text == "Dépendances"
    assert unit.blocks == (h(3, "Question : Qu'est-ce que Depends ?"), p("Réponse."))


def test_without_front_matter_the_same_document_is_split_on_the_heuristic() -> None:
    document = doc(
        h(1, "Fiche"),
        h(2, "Dépendances"),
        h(3, "Question : Qu'est-ce que Depends ?"),
        p("Réponse."),
    )

    units = segment(document, QA)

    (qa,) = [u for u in units if u.unit_type is UnitType.QA]
    assert qa.question_text == "Qu'est-ce que Depends ?"
    assert qa.section_path == SectionPath(("Fiche", "Dépendances"))


def test_questions_nested_in_topic_sections_get_the_topic_in_their_path() -> None:
    document = doc(
        h(1, "Fiche FastAPI"),
        h(2, "Dépendances"),
        h(3, "Question : Qu'est-ce que Depends ?"),
        p("Injection."),
        h(3, "Q: Comment tester une dépendance ?"),
        p("dependency_overrides."),
        h(2, "Démarrage"),
        h(3, "Question : Qu'est-ce que lifespan ?"),
        p("Cycle de vie."),
    )

    units = segment(document, QA)

    assert [(u.question_text, str(u.section_path)) for u in units] == [
        ("Qu'est-ce que Depends ?", "Fiche FastAPI > Dépendances"),
        ("Comment tester une dépendance ?", "Fiche FastAPI > Dépendances"),
        ("Qu'est-ce que lifespan ?", "Fiche FastAPI > Démarrage"),
    ]
    assert units[0].blocks == (p("Injection."),)


def test_an_unnumbered_answer_heading_at_the_question_level_stays_in_the_unit() -> None:
    document = doc(
        h(1, "Fiche"),
        h(2, "Question : Qu'est-ce que le GIL ?"),
        h(2, "Réponse"),
        p("Un verrou global de l'interpréteur."),
    )

    (unit,) = segment(document, QA)

    assert unit.blocks == (h(2, "Réponse"), p("Un verrou global de l'interpréteur."))


def test_content_before_the_first_question_is_a_section_unit() -> None:
    document = doc(
        h(1, "Fiche FastAPI"),
        p("Cette fiche couvre les bases."),
        h(2, "Question : Qu'est-ce que Depends ?"),
        p("Injection."),
    )

    units = segment(document, QA)

    assert [u.unit_type for u in units] == [UnitType.SECTION, UnitType.QA]
    assert units[0].question_text is None
    assert units[0].section_path == SectionPath(("Fiche FastAPI",))
    assert units[0].blocks == (p("Cette fiche couvre les bases."),)
    assert [u.ordinal for u in units] == [0, 1]


def test_a_unit_counts_the_tokens_of_its_rendered_blocks() -> None:
    document = doc(
        h(1, "Fiche"),
        h(2, "Question : Combien ?"),
        p(words(12)),
        code("a = 1\nb = 2"),
    )

    (unit,) = segment(document, QA)

    # 12 mots + "```python" "a" "=" "1" "b" "=" "2" "```"
    assert unit.token_count == COUNTER.count(
        words(12) + "\n\n```python\na = 1\nb = 2\n```"
    )


def test_the_page_span_of_a_unit_covers_its_blocks() -> None:
    document = doc(
        h(1, "Fiche", page=1),
        h(2, "Question : Où ?", page=2),
        p("a", page=2),
        CodeBlock(text="x = 1", language="python", page=3),
        p("sans page"),
    )

    (unit,) = segment(document, QA)

    assert unit.page_span == PageSpan(2, 3)


def test_a_unit_without_page_information_has_an_unknown_page_span() -> None:
    (unit,) = segment(doc(h(1, "F"), h(2, "Quoi ?"), p("x")), QA)

    assert unit.page_span == PageSpan(None, None)
