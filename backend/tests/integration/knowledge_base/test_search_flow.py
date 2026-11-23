"""Slice S5, bout en bout (knowledge-base.md §11, §12, §14 ; ADR 0010 §4-7) :
documents importés par l'API, extraits, indexés, publiés puis recherchés par
``KnowledgeSearch`` ET par ``POST /api/v1/knowledge-base/search``, sur
PostgreSQL et Qdrant réels, avec le VRAI modèle ``multilingual-e5-base``.

Pipeline des fixtures : ``MarkdownLiteParser`` (double de Docling), compteur
« un mot = un token », politique ``itest-1`` à 120 tokens (``extraction_support``).
La qualité de la récupération sur un corpus réaliste est mesurée à part
(``test_retrieval_evaluation``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import pytest

from knowledge_base.contracts.result import KnowledgeQuery
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.indexing_support import QA_SHEET, QA_SHEET_V2
from tests.integration.knowledge_base.search_support import (
    SearchRig,
    fixture_bytes,
    open_search_rig,
)

pytestmark = [pytest.mark.integration, pytest.mark.model]

PREFIX = "/api/v1/knowledge-base"
DEPENDS = "Qu'est-ce que Depends dans FastAPI ?"
# Budget de passage du test de troncature : au moins un chunk entier de la
# fixture (92 tokens au plus, mesurés), moins que son unité la plus longue
# (118 tokens de blocs) : cette unité est rendue en fenêtre d'un seul chunk.
BUDGET = 100


@pytest.fixture
async def rig(
    integration_settings: Settings,
    session_factory: SessionFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[SearchRig]:
    async with open_search_rig(
        integration_settings, session_factory, tmp_path, monkeypatch
    ) as opened:
        yield opened


async def publish_fastapi(rig: SearchRig) -> UUID:
    published = await rig.publish(
        fixture_bytes("qa_python_fastapi_fr.md"),
        title="Fiche de révision FastAPI",
        topics=("python", "fastapi"),
        filename="qa_python_fastapi_fr.md",
    )
    return published.document_id


# --- le cas qui porte la valeur -----------------------------------------------


async def test_a_published_question_is_found_with_its_complete_citation(
    rig: SearchRig,
) -> None:
    imported = await rig.publish(
        fixture_bytes("qa_python_fastapi_fr.md"),
        title="Titre importé de la fiche",  # distinct du titre H1 du fichier
        topics=("python", "fastapi"),
        filename="qa_python_fastapi_fr.md",
    )

    result = await rig.search.search(KnowledgeQuery(text="À quoi sert Depends ?"))

    best = result.passages[0]
    assert best.question == DEPENDS
    assert best.unit_type == "QA"
    citation = best.citation
    assert citation.document_id == imported.document_id
    assert citation.version_id == imported.version_id
    assert citation.version_number == 1
    assert citation.document_title == "Titre importé de la fiche"
    assert citation.source_filename == "qa_python_fastapi_fr.md"
    assert citation.section_path == ("Fiche de révision FastAPI",)
    assert (citation.page_start, citation.page_end) == (None, None)  # Markdown


async def test_the_passage_is_the_whole_unit_question_answer_and_code(
    rig: SearchRig,
) -> None:
    await publish_fastapi(rig)

    result = await rig.search.search(KnowledgeQuery(text="À quoi sert Depends ?"))

    best = result.passages[0]
    assert best.truncated is False
    assert DEPENDS in best.text
    assert "Depends déclare une dépendance" in best.text
    assert "def get_db" in best.text
    assert best.text.index(DEPENDS) < best.text.index("Depends déclare")


async def test_the_result_names_the_active_index_and_scores_decrease(
    rig: SearchRig,
) -> None:
    await publish_fastapi(rig)

    result = await rig.search.search(KnowledgeQuery(text="Depends", limit=3))

    assert result.index_id == rig.index_id
    assert len(result.passages) == 3
    assert result.complete is True
    scores = [p.score for p in result.passages]
    assert scores == sorted(scores, reverse=True)
    assert len({p.unit_id for p in result.passages}) == 3


async def test_the_limit_caps_the_number_of_passages(rig: SearchRig) -> None:
    await publish_fastapi(rig)

    result = await rig.search.search(KnowledgeQuery(text="FastAPI", limit=1))

    assert len(result.passages) == 1


async def test_a_search_without_any_published_document_finds_nothing(
    rig: SearchRig,
) -> None:
    result = await rig.search.search(KnowledgeQuery(text="Depends"))

    assert result.passages == ()
    assert result.index_id == rig.index_id


# --- filtres ------------------------------------------------------------------


async def publish_three_documents(rig: SearchRig) -> dict[str, UUID]:
    fastapi = await publish_fastapi(rig)
    asyncio = await rig.publish(
        fixture_bytes("cours_asyncio_fr.md"),
        title="Cours asyncio",
        kind="COURSE",
        topics=("python", "asyncio"),
        filename="cours_asyncio_fr.md",
    )
    english = await rig.publish(
        fixture_bytes("qa_python_en.md"),
        title="Python interview notes",
        language="en",
        topics=("python",),
        filename="qa_python_en.md",
    )
    return {
        "fastapi": fastapi,
        "asyncio": asyncio.document_id,
        "english": english.document_id,
    }


@pytest.mark.parametrize(
    ("filters", "allowed"),
    [
        ({"language": "en"}, {"english"}),
        ({"topics": ("asyncio",)}, {"asyncio"}),
        ({"kinds": ("COURSE",)}, {"asyncio"}),
        ({"topics": ("python",), "language": "fr"}, {"fastapi", "asyncio"}),
    ],
    ids=["language", "topic", "kind", "topic-and-language"],
)
async def test_the_filters_restrict_the_results_to_the_matching_documents(
    rig: SearchRig, filters: dict[str, object], allowed: set[str]
) -> None:
    documents = await publish_three_documents(rig)

    result = await rig.search.search(
        KnowledgeQuery(text="coroutine and dependency", limit=5, **filters)  # type: ignore[arg-type]
    )

    assert result.passages
    found = {p.citation.document_id for p in result.passages}
    assert found <= {documents[name] for name in allowed}


async def test_a_document_filter_returns_only_that_document(rig: SearchRig) -> None:
    documents = await publish_three_documents(rig)

    result = await rig.search.search(
        KnowledgeQuery(text="python", document_ids=(documents["english"],))
    )

    assert result.passages
    assert {p.citation.document_id for p in result.passages} == {documents["english"]}


async def test_filters_that_match_no_document_give_an_empty_result(
    rig: SearchRig,
) -> None:
    await publish_three_documents(rig)

    result = await rig.search.search(
        KnowledgeQuery(text="python", topics=("kubernetes",))
    )

    assert result.passages == ()


# --- un document non publié n'est jamais renvoyé ------------------------------


async def test_an_extracted_but_unpublished_version_is_never_returned(
    rig: SearchRig,
) -> None:
    # Extraite, jamais indexée ni publiée : aucun point, aucune publication.
    await rig.import_only(QA_SHEET)

    result = await rig.search.search(KnowledgeQuery(text="Depends"))

    assert result.passages == ()


async def test_a_version_indexed_but_not_yet_synchronized_is_not_returned(
    rig: SearchRig,
) -> None:
    # Publiée en SQL, points encore ``visible=false`` (fenêtre du §11) : le
    # document manque temporairement, il ne peut pas apparaître à tort.
    imported = await rig.import_only(QA_SHEET)
    await rig.index_and_publish(imported)

    result = await rig.search.search(KnowledgeQuery(text="Depends"))

    assert result.passages == ()


async def test_a_superseded_version_still_visible_in_qdrant_is_never_returned(
    rig: SearchRig,
) -> None:
    first = await rig.publish(QA_SHEET)
    second = await rig.indexing.import_version_and_extract(
        first.document_id, QA_SHEET_V2
    )
    # La version 2 est publiée en SQL ; Qdrant garde la 1 visible et la 2
    # invisible tant que ``sync_visibility`` n'a pas convergé.
    await rig.index_and_publish(second)
    in_qdrant = {
        p.payload["version_id"]: p.payload["visible"]
        for p in await rig.indexing.points(rig.index_id)
        if p.payload is not None
    }
    assert in_qdrant == {str(first.version_id): True, str(second.version_id): False}

    window = await rig.search.search(KnowledgeQuery(text="Depends injecte"))

    assert str(first.version_id) not in {
        str(p.citation.version_id) for p in window.passages
    }
    assert window.passages == ()  # la version 2 n'est pas encore visible

    await rig.synchronize(first.document_id)
    after = await rig.search.search(KnowledgeQuery(text="Depends résout"))

    assert after.passages
    assert {p.citation.version_id for p in after.passages} == {second.version_id}
    assert {p.citation.version_number for p in after.passages} == {2}


async def test_a_withdrawn_document_is_never_returned_even_if_qdrant_still_shows_it(
    rig: SearchRig,
) -> None:
    imported = await rig.publish(QA_SHEET)
    assert (await rig.search.search(KnowledgeQuery(text="Depends"))).passages
    # Retrait validé en SQL, ``sync_visibility`` pas encore passé (S7 livrera
    # la commande de retrait ; PostgreSQL arbitre dès le commit, ADR 0010 §8).
    await rig.execute(
        "UPDATE knowledge_base.documents SET status = 'WITHDRAWN' WHERE id = :id",
        id=imported.document_id,
    )

    result = await rig.search.search(KnowledgeQuery(text="Depends"))

    assert result.passages == ()


async def test_when_rejections_exceed_the_complement_the_result_is_incomplete(
    rig: SearchRig,
) -> None:
    # Un document actif (3 unités) et un document retiré en SQL dont les points
    # sont encore visibles : ils occupent le haut du classement.
    kept = await rig.publish(
        QA_SHEET.replace(b"Depends", b"Resolver"),
        title="Active",
        filename="active.md",
    )
    gone = await rig.publish(QA_SHEET, title="Retirée", filename="retiree.md")
    await rig.execute(
        "UPDATE knowledge_base.documents SET status = 'WITHDRAWN' WHERE id = :id",
        id=gone.document_id,
    )

    result = await rig.search.search(
        KnowledgeQuery(text="Qu'est-ce que Depends ?", limit=5)
    )

    assert {p.citation.document_id for p in result.passages} == {kept.document_id}
    assert len(result.passages) == 3
    assert result.complete is False  # moins de ``limit`` passages (§14)


# --- small-to-big -------------------------------------------------------------------


async def test_a_long_unit_is_truncated_around_the_matched_chunk(
    integration_settings: Settings,
    session_factory: SessionFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with open_search_rig(
        integration_settings,
        session_factory,
        tmp_path,
        monkeypatch,
        max_passage_tokens=BUDGET,
    ) as rig:
        imported = await rig.publish(
            fixture_bytes("qa_python_fastapi_fr.md"), filename="qa_python_fastapi_fr.md"
        )
        chunks = await rig.indexing.extraction.chunks(imported.version_id)
        units = await rig.indexing.extraction.units(imported.version_id)
        (long_unit,) = [u for u in units if u["token_count"] > BUDGET]
        unit_chunks = [c for c in chunks if c["unit_id"] == long_unit["id"]]
        assert len(unit_chunks) >= 2, "la fixture doit produire une unité en 2 chunks"
        assert all(c["token_count"] <= BUDGET for c in unit_chunks)

        result = await rig.search.search(
            KnowledgeQuery(
                text="Pourquoi vider dependency_overrides à la fin de chaque test ?"
            )
        )

        passage = next(p for p in result.passages if p.unit_id == long_unit["id"])
        assert passage.truncated is True
        assert long_unit["question_text"] in passage.text
        full = long_unit["text"]
        assert len(passage.text) < len(long_unit["question_text"]) + len(full) + 2
        matched = [c for c in unit_chunks if c["id"] in passage.matched_chunk_ids]
        assert matched
        # La fenêtre contient au moins un des chunks trouvés (elle ne garantit
        # pas les autres : ils peuvent ne pas tenir dans le budget).
        assert any(
            chunk["contextual_text"].rstrip()[-40:] in passage.text for chunk in matched
        )


# --- REST ---------------------------------------------------------------------


async def test_the_rest_endpoint_serves_the_same_search_as_the_public_contract(
    rig: SearchRig,
) -> None:
    imported = await rig.publish(
        fixture_bytes("qa_python_fastapi_fr.md"),
        title="Fiche de révision FastAPI",
        topics=("python", "fastapi"),
        filename="qa_python_fastapi_fr.md",
    )
    direct = await rig.search.search(KnowledgeQuery(text="À quoi sert Depends ?"))

    response = await rig.http.post(
        f"{PREFIX}/search", json={"text": "À quoi sert Depends ?"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["index_id"] == str(rig.index_id)
    assert body["complete"] == direct.complete
    assert [p["unit_id"] for p in body["passages"]] == [
        str(p.unit_id) for p in direct.passages
    ]
    first = body["passages"][0]
    assert first["question"] == DEPENDS
    assert first["citation"]["document_id"] == str(imported.document_id)
    assert first["citation"]["version_id"] == str(imported.version_id)
    assert first["citation"]["version_number"] == 1
    assert first["citation"]["source_filename"] == "qa_python_fastapi_fr.md"
    assert "def get_db" in first["text"]


async def test_the_rest_endpoint_applies_the_filters_of_the_body(
    rig: SearchRig,
) -> None:
    documents = await publish_three_documents(rig)

    response = await rig.http.post(
        f"{PREFIX}/search",
        json={"text": "python", "language": "en", "limit": 3},
    )

    assert response.status_code == 200
    found = {p["citation"]["document_id"] for p in response.json()["passages"]}
    assert found == {str(documents["english"])}


async def test_the_rest_endpoint_refuses_an_empty_query(rig: SearchRig) -> None:
    response = await rig.http.post(f"{PREFIX}/search", json={"text": " "})

    assert response.status_code == 422
