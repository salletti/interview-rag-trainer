"""``ChunkPlanner`` : budget en tokens, frontières de blocs, fallbacks
code/tableau, citation (ADR 0009 §3-5, knowledge-base.md §15.2, I13).

API figée (``knowledge_base.domain.services.chunk_planner``) :

- ``ChunkPlanner(token_counter, policy)`` ; ``plan(unit, citation_base:
  Citation) -> Sequence[DocumentChunk]``. ``citation_base`` porte ce que
  l'unité ne sait pas (document, titre, version, numéro, fichier) ; le
  planificateur y met ``section_path`` de l'unité et les pages des blocs du
  chunk.
- ``CONTINUATION_MARKER`` : constante du module, courte (6 mots au plus).
- ``contextual_text`` = ``en-tête + "\\n\\n" + corps`` ; l'en-tête est le titre du
  document, puis le chemin de sections (`` > ``) s'il n'est pas vide, puis
  ``Question : <question>`` si l'unité en a une, une ligne chacun.
  ``corps = render_blocks(blocs du chunk)``.
- ``token_count`` du chunk = tokens de son ``contextual_text`` COMPLET (en-tête
  et marqueur compris) ; il ne dépasse jamais ``policy.max_tokens``.
- Remplissage séquentiel : les blocs entiers sont ajoutés au chunk courant tant
  que l'ensemble tient ; sinon le chunk est clos et le bloc ouvre le suivant.
- Un bloc ``CodeBlock`` ou ``Table`` qui, seul, ne tient pas est découpé :
  pour le code, aux lignes vides, puis aux lignes commençant par ``def`` /
  ``class`` (colonne 0), puis ligne à ligne en dernier recours ; pour un
  tableau, ligne à ligne. Chaque fragment est un chunk avec l'en-tête ; tout
  fragment sauf le premier commence son corps par ``CONTINUATION_MARKER``,
  seul sur sa ligne.
- ``chunk_hash`` = SHA-256 hexadécimal de ``contextual_text + policy.version``.
- ``chunk_id`` : UUIDv5 déterministe de (``version_id`` de la citation,
  ``policy.version``, ``unit.ordinal``, ``ordinal_in_unit``).
- Non figé ici : paragraphe ou liste seuls plus grands que le budget ;
  ``overlap_tokens > 0`` ; en-tête seul plus grand que le budget.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from uuid import UUID

import pytest

from knowledge_base.domain.services.chunk_planner import CONTINUATION_MARKER
from knowledge_base.domain.value_objects.citation import Citation
from knowledge_base.domain.value_objects.document_chunk import DocumentChunk
from knowledge_base.domain.value_objects.knowledge_unit import KnowledgeUnit, UnitType
from knowledge_base.domain.value_objects.section_path import SectionPath
from knowledge_base.domain.value_objects.structured_document import (
    Table,
    render_blocks,
)
from tests.unit.knowledge_base.domain.services.support import (
    COUNTER,
    DOCUMENT_ID,
    FILENAME,
    TITLE,
    VERSION_ID,
    code,
    h,
    header,
    make_unit,
    p,
    plan,
    with_header,
    words,
)

QUESTION = "Qu'est-ce que Depends ?"
PATH = ("FastAPI", "Dépendances")


def head_tokens(head: str) -> int:
    return COUNTER.count(head)


def texts(chunks: Sequence[DocumentChunk]) -> list[str]:
    return [c.contextual_text for c in chunks]


def kept_lines(chunks: Sequence[DocumentChunk], source: Sequence[str]) -> list[str]:
    """Lignes de ``source`` retrouvées dans les chunks, dans l'ordre des chunks."""
    known = set(source)
    return [
        line
        for chunk in chunks
        for line in chunk.contextual_text.splitlines()
        if line in known
    ]


def test_the_continuation_marker_is_short() -> None:
    assert 0 < COUNTER.count(CONTINUATION_MARKER) <= 6


# --- une unité qui tient : un chunk -


def test_a_unit_within_the_budget_is_a_single_chunk_with_its_context_header() -> None:
    unit = make_unit(
        p("Depends déclare une dépendance."),
        code("def f(db=Depends(get_db)): ..."),
        unit_type=UnitType.QA,
        question=QUESTION,
        path=PATH,
    )

    (chunk,) = plan(unit, max_tokens=1000)

    expected = with_header(header(TITLE, PATH, QUESTION), render_blocks(unit.blocks))
    assert chunk.contextual_text == expected
    assert chunk.unit_id == unit.unit_id
    assert chunk.ordinal_in_unit == 0
    assert chunk.token_count == COUNTER.count(expected)


def test_the_header_of_a_unit_without_question_or_path_is_the_title_only() -> None:
    unit = make_unit(p("texte"), path=())

    (chunk,) = plan(unit)

    assert chunk.contextual_text == f"{TITLE}\n\ntexte"


def test_a_unit_exactly_at_the_budget_is_one_chunk_and_one_token_less_is_two() -> None:
    unit = make_unit(p(words(10, "a")), p(words(10, "b")), path=PATH)
    full = COUNTER.count(with_header(header(TITLE, PATH), render_blocks(unit.blocks)))

    assert len(plan(unit, max_tokens=full)) == 1
    assert len(plan(unit, max_tokens=full - 1)) == 2


def test_chunk_hash_is_the_sha256_of_the_text_followed_by_the_policy_version() -> None:
    unit = make_unit(p("texte"))

    (chunk,) = plan(unit, policy_version="politique-7")

    digest = hashlib.sha256(
        (chunk.contextual_text + "politique-7").encode()
    ).hexdigest()
    assert chunk.chunk_hash == digest


# --- une unité trop longue : découpe aux frontières de blocs -


def test_an_oversized_unit_is_cut_between_blocks_and_repeats_the_header() -> None:
    paragraphs = [p(words(20, f"p{i}_")) for i in range(6)]
    unit = make_unit(*paragraphs, unit_type=UnitType.QA, question=QUESTION, path=PATH)
    head = header(TITLE, PATH, QUESTION)
    # En-tête + deux paragraphes tiennent ; trois non.
    budget = head_tokens(head) + 40 + 5

    chunks = plan(unit, max_tokens=budget)

    assert texts(chunks) == [
        with_header(head, render_blocks(paragraphs[0:2])),
        with_header(head, render_blocks(paragraphs[2:4])),
        with_header(head, render_blocks(paragraphs[4:6])),
    ]
    assert [c.ordinal_in_unit for c in chunks] == [0, 1, 2]
    assert all(QUESTION in c.contextual_text for c in chunks)
    assert all(c.unit_id == unit.unit_id for c in chunks)


def test_no_chunk_exceeds_the_budget_whatever_the_blocks_and_the_budget() -> None:
    table = Table(text="\n".join(f"r{i} a{i} b{i} c{i}" for i in range(12)))
    big_code = code("\n".join(f"v{i} = g{i}(x)" for i in range(30)))
    unit = make_unit(
        p(words(8, "a")),
        big_code,
        p(words(8, "b")),
        table,
        h(3, "Détail"),
        p(words(8, "c")),
        unit_type=UnitType.QA,
        question=QUESTION,
        path=PATH,
    )

    for budget in (30, 40, 60, 100, 400):
        chunks = plan(unit, max_tokens=budget)
        for chunk in chunks:
            assert chunk.token_count == COUNTER.count(chunk.contextual_text)
            assert chunk.token_count <= budget, f"budget {budget}"
        assert [c.ordinal_in_unit for c in chunks] == list(range(len(chunks)))


def test_every_paragraph_survives_whole_in_exactly_one_chunk() -> None:
    paragraphs = [p(words(9, f"q{i}_")) for i in range(7)]
    unit = make_unit(*paragraphs, code("\n".join(f"l{i} = {i}" for i in range(20))))

    chunks = plan(unit, max_tokens=40)

    for paragraph in paragraphs:
        owners = [c for c in chunks if paragraph.text in c.contextual_text]
        assert len(owners) == 1


# --- code surdimensionné -


def function(index: int) -> list[str]:
    return [f"def func_{index}(x):"] + [
        f"    step_{index}_{i} = compute_{index}_{i}(x)" for i in range(5)
    ]


def code_budget(head: str, lines: int = 17) -> int:
    """Budget d'un groupe de ``lines`` mots + barrières (2) + marqueur, avec une
    marge inférieure à un groupe supplémentaire."""
    return head_tokens(head) + lines + 2 + COUNTER.count(CONTINUATION_MARKER) + 3


def test_an_oversized_code_block_is_cut_at_blank_lines_and_keeps_its_groups_whole() -> (
    None
):
    groups = [
        function(i)[1:] for i in range(3)
    ]  # sans ``def`` : seules les lignes vides séparent
    source = [line for group in groups for line in group]
    block = code("\n\n".join("\n".join(group) for group in groups))
    head = header(TITLE, ("Cours",))
    unit = make_unit(block)

    chunks = plan(unit, max_tokens=code_budget(head, lines=15))

    assert len(chunks) == 3
    for chunk, group in zip(chunks, groups, strict=True):
        assert [
            line for line in chunk.contextual_text.splitlines() if line in set(source)
        ] == group


@pytest.mark.parametrize("keyword", ["def", "class"])
def test_an_oversized_code_block_without_blank_lines_is_cut_at_definitions(
    keyword: str,
) -> None:
    groups = [
        [f"{keyword} Unit{i}(x):"]
        + [f"    step_{i}_{j} = compute_{i}_{j}(x)" for j in range(5)]
        for i in range(3)
    ]
    source = [line for group in groups for line in group]
    unit = make_unit(code("\n".join(source)))
    head = header(TITLE, ("Cours",))

    chunks = plan(unit, max_tokens=code_budget(head))

    assert len(chunks) == 3
    for chunk, group in zip(chunks, groups, strict=True):
        assert [
            line for line in chunk.contextual_text.splitlines() if line in set(source)
        ] == group


def test_a_code_block_without_any_boundary_is_cut_line_by_line_losing_nothing() -> None:
    source = [f"    value_{i} = transform_{i}(data)" for i in range(40)]
    unit = make_unit(code("\n".join(source)))

    chunks = plan(unit, max_tokens=30)

    assert len(chunks) > 2
    assert kept_lines(chunks, source) == source
    assert all(c.token_count <= 30 for c in chunks)


def test_every_fragment_but_the_first_starts_with_the_continuation_marker() -> None:
    source = [f"    value_{i} = transform_{i}(data)" for i in range(40)]
    unit = make_unit(code("\n".join(source)))
    head = header(TITLE, ("Cours",))

    chunks = plan(unit, max_tokens=30)

    prefix = head + "\n\n"
    assert (
        not chunks[0]
        .contextual_text.removeprefix(prefix)
        .startswith(CONTINUATION_MARKER)
    )
    for chunk in chunks[1:]:
        assert chunk.contextual_text.startswith(prefix + CONTINUATION_MARKER + "\n")
        assert chunk.contextual_text.startswith(head)


def test_a_code_block_that_fits_is_never_cut() -> None:
    block = code("\n".join([*function(0), "", *function(1)]))
    unit = make_unit(block)

    (chunk,) = plan(unit, max_tokens=1000)

    assert chunk.contextual_text == with_header(
        header(TITLE, ("Cours",)), render_blocks((block,))
    )


# --- tableaux -


ROWS = [f"r{i} a{i} b{i} c{i}" for i in range(5)]


def test_a_table_within_the_budget_stays_atomic() -> None:
    first, table, last = (
        p(words(10, "x")),
        Table(text="\n".join(ROWS)),
        p(words(10, "y")),
    )
    unit = make_unit(first, table, last)
    head = header(TITLE, ("Cours",))
    # Le tableau (20) tient seul ; avec un paragraphe voisin (10) non.
    budget = head_tokens(head) + 20 + 5

    chunks = plan(unit, max_tokens=budget)

    assert texts(chunks) == [
        with_header(head, first.text),
        with_header(head, table.text),
        with_header(head, last.text),
    ]


def test_an_oversized_table_is_cut_by_rows_with_continuation_markers() -> None:
    rows = [f"r{i} a{i} b{i} c{i}" for i in range(12)]
    unit = make_unit(Table(text="\n".join(rows)))
    head = header(TITLE, ("Cours",))
    budget = head_tokens(head) + COUNTER.count(CONTINUATION_MARKER) + 4 * 3 + 1

    chunks = plan(unit, max_tokens=budget)

    assert len(chunks) == 4
    assert kept_lines(chunks, rows) == rows
    assert all(c.token_count <= budget for c in chunks)
    assert sum(CONTINUATION_MARKER in c.contextual_text for c in chunks) == 3
    assert CONTINUATION_MARKER not in chunks[0].contextual_text


# --- citation (I13) -


def test_every_chunk_carries_a_complete_citation() -> None:
    unit = make_unit(
        p(words(20, "a"), page=2),
        p(words(20, "b"), page=3),
        p(words(20, "c"), page=5),
        unit_type=UnitType.QA,
        question=QUESTION,
        path=PATH,
    )
    head = header(TITLE, PATH, QUESTION)

    chunks = plan(unit, max_tokens=head_tokens(head) + 40 + 5)

    expected_base = {
        "document_id": DOCUMENT_ID,
        "document_title": TITLE,
        "version_id": VERSION_ID,
        "version_number": 3,
        "section_path": SectionPath(PATH),
        "source_filename": FILENAME,
    }
    assert [(c.citation.page_start, c.citation.page_end) for c in chunks] == [
        (2, 3),
        (5, 5),
    ]
    for chunk in chunks:
        for field, value in expected_base.items():
            assert getattr(chunk.citation, field) == value, field


def test_a_unit_without_pages_gives_citations_without_pages() -> None:
    (chunk,) = plan(make_unit(p("texte")))

    assert isinstance(chunk.citation, Citation)
    assert (chunk.citation.page_start, chunk.citation.page_end) == (None, None)


def test_the_page_span_of_a_split_code_block_covers_its_page() -> None:
    unit = make_unit(code("\n".join(f"v{i} = f{i}(x)" for i in range(30)), page=7))

    chunks = plan(unit, max_tokens=30)

    assert len(chunks) > 1
    assert {(c.citation.page_start, c.citation.page_end) for c in chunks} == {(7, 7)}


# --- identifiants déterministes -


TWO_CHUNKS = 30  # budget : en-tête (3) + un paragraphe (20) tient, deux non


def two_chunk_unit(ordinal: int = 0) -> KnowledgeUnit:
    return make_unit(p(words(20, "a")), p(words(20, "b")), ordinal=ordinal)


def test_two_runs_produce_the_same_ids_texts_and_hashes() -> None:
    unit = two_chunk_unit()

    first = plan(unit, max_tokens=TWO_CHUNKS)
    second = plan(unit, max_tokens=TWO_CHUNKS)

    assert len(first) == 2
    assert first == second


def test_chunk_ids_are_uuid5_and_distinct() -> None:
    unit = two_chunk_unit()

    ids = [c.chunk_id for c in plan(unit, max_tokens=TWO_CHUNKS)]

    assert len(set(ids)) == 2
    assert all(isinstance(i, UUID) and i.version == 5 for i in ids)


def test_chunk_ids_depend_on_the_version_the_policy_and_the_unit_ordinal() -> None:
    unit = two_chunk_unit()
    other_unit = two_chunk_unit(ordinal=1)
    reference = {c.chunk_id for c in plan(unit, max_tokens=TWO_CHUNKS)}

    other_version = plan(unit, max_tokens=TWO_CHUNKS, version_id=UUID(int=99))
    other_policy = plan(unit, max_tokens=TWO_CHUNKS, policy_version="autre")
    other_ordinal = plan(other_unit, max_tokens=TWO_CHUNKS)

    for variant in (other_version, other_policy, other_ordinal):
        assert reference.isdisjoint({c.chunk_id for c in variant})


def test_the_chunk_hash_changes_with_the_policy_version_for_the_same_text() -> None:
    unit = make_unit(p("texte"))

    (a,) = plan(unit, policy_version="v1")
    (b,) = plan(unit, policy_version="v2")

    assert a.contextual_text == b.contextual_text
    assert a.chunk_hash != b.chunk_hash
