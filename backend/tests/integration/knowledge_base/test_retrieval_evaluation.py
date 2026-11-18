"""Évaluation de la récupération (S5, marqueur ``retrieval``) : mesure de
référence et non-régression (roadmap S5 ; ADR 0009 et 0010, conditions de
réévaluation ; knowledge-base.md §12, O12).

Pipeline de PRODUCTION, sur services réels : Docling (Markdown), tokenizer
réel et politique ``DEFAULT_CHUNKING_POLICY`` (``kb-chunking-v1+xlmr-…``),
vrai modèle ``multilingual-e5-base``, Qdrant et PostgreSQL, budget de passage
par défaut de ``KnowledgeBaseSettings``. Le corpus (10 documents FR/EN, fiches
Q/R, cours et fiches de révision, dont 2 distracteurs) est indexé UNE fois par
module ; les requêtes de ``fixtures/knowledge_base/retrieval/
retrieval_reference.json`` sont rejouées sans filtre puis avec leurs filtres.

Métriques (définitions dans ``retrieval_support``) : Recall@1, Recall@5, MRR,
intégrité d'unité, Recall@5 des requêtes inter-langues (FR -> EN et EN -> FR),
Recall@5 avec et sans filtres. Chaque exécution ÉCRIT sa mesure dans
``$KB_RETRIEVAL_REPORT_DIR`` (défaut : ``backend/.cache/retrieval/``,
ignoré par git) : c'est la « mesure de référence enregistrée » du critère
d'acceptation de S5.

SEUILS (``retrieval_thresholds.json``) : NON FIXÉS tant que ``minimum`` vaut
``null`` ; le test correspondant ÉCHOUE alors en affichant la valeur mesurée,
pour qu'un seuil vide ne rende jamais la suite verte. À remplir après la
première mesure, avec une marge documentée. Indépendamment du fichier, des
PLANCHERS DE PLAUSIBILITÉ (``FLOORS``) détectent une recherche cassée
(hasard : environ 10 % de Recall@5) ; ils ne remplacent pas les seuils mesurés.

Les invariants qui ne dépendent d'aucune mesure (citation complète, filtres
respectés, unité complète intègre) sont des tests séparés, sans seuil.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest
import pytest_asyncio

from knowledge_base.adapters.controller.composition import compose_knowledge_search
from knowledge_base.adapters.controller.tasks.extract_version_handler import (
    build_extract_version_handler,
)
from knowledge_base.adapters.gateway.embedding.sentence_transformers_embedder import (
    SentenceTransformersEmbedder,
)
from knowledge_base.contracts.providers import KnowledgeSearch
from knowledge_base.contracts.result import KnowledgeQuery, KnowledgeSearchResult
from knowledge_base.domain.value_objects.embedding_profile import MULTILINGUAL_E5_BASE
from knowledge_base.frameworks.configuration.chunking import DEFAULT_CHUNKING_POLICY
from knowledge_base.use_cases.provision_search_index.provision_search_index_command import (  # noqa: E501
    ProvisionSearchIndex,
)
from runtime.database import create_engine, create_session_factory
from runtime.settings import Settings
from tests.integration.knowledge_base.extraction_support import Imported
from tests.integration.knowledge_base.indexing_support import (
    IndexingRig,
    open_indexing_rig,
)
from tests.integration.knowledge_base.retrieval_support import (
    THRESHOLDS,
    Reference,
    ReferenceQuery,
    load_reference,
    mean_reciprocal_rank,
    rank_of_expected,
    recall_at_k,
    unit_integrity,
    unit_is_intact,
)
from tests.integration.knowledge_base.search_support import models_cache
from tests.integration.knowledge_base.support import kb_settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.model,
    pytest.mark.retrieval,
    pytest.mark.asyncio(loop_scope="module"),
]

RESULT_LIMIT = 10
BACKEND = Path(__file__).resolve().parents[3]

# Planchers de plausibilité, PAS des seuils de non-régression (voir docstring).
FLOORS = {
    "recall_at_5": 0.5,
    "mrr": 0.3,
    "unit_integrity": 0.5,
    "cross_language_recall_at_5": 0.3,
    "recall_at_5_with_filters": 0.5,
}


def report_dir() -> Path:
    return Path(
        os.environ.get("KB_RETRIEVAL_REPORT_DIR", BACKEND / ".cache" / "retrieval")
    )


# --- corpus et mesure --------------------------------------------------------


@dataclass(frozen=True)
class QueryOutcome:
    query: ReferenceQuery
    plain: KnowledgeSearchResult
    filtered: KnowledgeSearchResult | None
    plain_rank: int | None
    filtered_rank: int | None
    expected_text: str | None  # texte du passage attendu dans les 5 premiers


@dataclass(frozen=True)
class Evaluation:
    reference: Reference
    outcomes: tuple[QueryOutcome, ...]
    imported: dict[str, Imported]
    rig: IndexingRig
    index_id: UUID

    # --- métriques ---

    def ranks(
        self, *, filtered: bool = False, cross: bool | None = None
    ) -> list[int | None]:
        return [
            o.filtered_rank if filtered else o.plain_rank
            for o in self.outcomes
            if (not filtered or o.query.filters is not None)
            and (cross is None or o.query.cross_language == cross)
        ]

    def metrics(self) -> dict[str, float | None]:
        plain = self.ranks()
        with_filters = self.ranks(filtered=True)
        same_queries_plain = [
            o.plain_rank for o in self.outcomes if o.query.filters is not None
        ]
        return {
            "recall_at_1": recall_at_k(plain, 1),
            "recall_at_5": recall_at_k(plain, 5),
            "mrr": mean_reciprocal_rank(plain),
            "unit_integrity": unit_integrity(
                [(o.query.expected, o.expected_text) for o in self.outcomes]
            ),
            "cross_language_recall_at_5": recall_at_k(self.ranks(cross=True), 5),
            "same_language_recall_at_5": recall_at_k(self.ranks(cross=False), 5),
            "recall_at_5_with_filters": recall_at_k(with_filters, 5),
            "recall_at_5_without_filters_same_queries": recall_at_k(
                same_queries_plain, 5
            ),
        }


async def search_all(
    search: KnowledgeSearch, reference: Reference
) -> tuple[QueryOutcome, ...]:
    outcomes: list[QueryOutcome] = []
    for query in reference.queries:
        plain = await search.search(KnowledgeQuery(text=query.text, limit=RESULT_LIMIT))
        filtered = None
        if query.filters is not None:
            filtered = await search.search(
                KnowledgeQuery(
                    text=query.text,
                    limit=RESULT_LIMIT,
                    language=query.filters.language,
                    topics=query.filters.topics,
                    kinds=query.filters.kinds,
                )
            )
        expected = query.expected
        plain_rank = rank_of_expected(listing(plain), expected)
        outcomes.append(
            QueryOutcome(
                query=query,
                plain=plain,
                filtered=filtered,
                plain_rank=plain_rank,
                filtered_rank=(
                    None
                    if filtered is None
                    else rank_of_expected(listing(filtered), expected)
                ),
                expected_text=(
                    plain.passages[plain_rank - 1].text
                    if plain_rank is not None and plain_rank <= 5
                    else None
                ),
            )
        )
    return tuple(outcomes)


def listing(result: KnowledgeSearchResult) -> list[tuple[str, str]]:
    return [(p.citation.source_filename, p.text) for p in result.passages]


async def build_corpus(
    rig: IndexingRig, reference: Reference
) -> tuple[UUID, dict[str, Imported]]:
    """Chaque document est importé par l'API, extrait par le vrai Docling avec
    le tokenizer E5, indexé, publié et synchronisé."""
    embedder = rig.embedder
    assert isinstance(embedder, SentenceTransformersEmbedder)
    extract = build_extract_version_handler(
        rig.extraction.session_factory,
        rig.extraction.settings,
        token_counter=embedder.token_counter,
        policy=DEFAULT_CHUNKING_POLICY,
    )
    index_id = await rig.provisioner.handle(
        ProvisionSearchIndex(
            profile=embedder.profile, chunking_policy=DEFAULT_CHUNKING_POLICY
        )
    )
    imported: dict[str, Imported] = {}
    for document in reference.documents:
        response = await rig.extraction.imports.post_document(
            document.path.read_bytes(),
            filename=document.filename,
            title=document.title,
            kind=document.kind,
            language=document.language,
            topics=document.topics,
        )
        assert response.status_code == 202, (document.file, response.text)
        body = response.json()
        (item,) = [
            i
            for i in await rig.extraction.imports.extract_items()
            if i["payload"]["version_id"] == body["version_id"]
        ]
        found = Imported(
            document_id=UUID(body["document_id"]),
            version_id=UUID(body["version_id"]),
            item_id=item["id"],
        )
        assert await rig.extraction.run(found.item_id, extract) == "succeeded"
        assert await rig.run_index(found.version_id, index_id) == "succeeded"
        revision = (await rig.document(found.document_id))["visibility_revision"]
        assert await rig.run_sync(found.document_id, revision) == "succeeded"
        imported[document.file] = found
    return index_id, imported


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def evaluation(
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncIterator[Evaluation]:
    settings = Settings(environment="test", external_timeout_seconds=5.0)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    reference = load_reference()
    embedder = SentenceTransformersEmbedder.load(
        MULTILINGUAL_E5_BASE, cache_dir=models_cache()
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("KB_MODELS_CACHE_DIR", models_cache())
        async with open_indexing_rig(
            settings,
            session_factory,
            tmp_path_factory.mktemp("retrieval"),
            patch,
            embedder,
        ) as rig:
            index_id, imported = await build_corpus(rig, reference)
            search = compose_knowledge_search(
                session_factory,
                rig.qdrant,
                kb_settings(),
                embedder=embedder,
                token_counter=embedder.token_counter,
            )
            outcomes = await search_all(search, reference)
            evaluation = Evaluation(reference, outcomes, imported, rig, index_id)
            record(evaluation)
            yield evaluation
    await engine.dispose()


def record(evaluation: Evaluation) -> Path:
    """Écrit la mesure de référence (rang de chaque requête compris)."""
    directory = report_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "retrieval_report.json"
    path.write_text(
        json.dumps(
            {
                "pipeline": {
                    "profile": MULTILINGUAL_E5_BASE.model,
                    "revision": MULTILINGUAL_E5_BASE.revision,
                    "chunking_policy": DEFAULT_CHUNKING_POLICY.identity,
                    "result_limit": RESULT_LIMIT,
                },
                "documents": len(evaluation.reference.documents),
                "queries": len(evaluation.reference.queries),
                "metrics": evaluation.metrics(),
                "per_query": [
                    {
                        "id": o.query.id,
                        "cross_language": o.query.cross_language,
                        "rank": o.plain_rank,
                        "rank_with_filters": o.filtered_rank,
                        "passage_truncated": (
                            None
                            if o.plain_rank is None
                            else o.plain.passages[o.plain_rank - 1].truncated
                        ),
                        "complete": o.plain.complete,
                    }
                    for o in evaluation.outcomes
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def threshold_of(metric: str) -> float | None:
    raw = json.loads(THRESHOLDS.read_text(encoding="utf-8"))
    minimum = raw["metrics"][metric]["minimum"]
    return None if minimum is None else float(minimum)


def assert_meets(metric: str, measured: float | None) -> None:
    assert measured is not None, f"{metric} : aucune unité attendue retrouvée"
    assert measured >= FLOORS[metric], (
        f"{metric} = {measured:.3f} sous le plancher de plausibilité "
        f"{FLOORS[metric]} : la recherche est cassée"
    )
    minimum = threshold_of(metric)
    if minimum is None:
        pytest.fail(
            f"SEUIL NON FIXÉ pour {metric} (valeur mesurée : {measured:.3f}). "
            "Remplir `minimum` dans retrieval_thresholds.json à partir de la "
            "mesure de référence, avec une marge documentée."
        )
    assert measured >= minimum, (
        f"régression de {metric} : {measured:.3f} < seuil {minimum}"
    )


# --- le corpus de référence est bien en place --------------------------------


async def test_every_reference_document_is_published_and_visible_in_the_index(
    evaluation: Evaluation,
) -> None:
    rig = evaluation.rig
    for file, imported in evaluation.imported.items():
        version = await rig.extraction.version(imported.version_id)
        assert version["status"] == "PUBLISHED", file
    points = await rig.points(evaluation.index_id)
    assert points
    assert all(record_.payload and record_.payload["visible"] for record_ in points)
    expected = 0
    for imported in evaluation.imported.values():
        expected += len(await rig.extraction.chunks(imported.version_id))
    assert len(points) == expected


async def test_the_reference_measure_is_recorded(evaluation: Evaluation) -> None:
    report = json.loads(
        (report_dir() / "retrieval_report.json").read_text(encoding="utf-8")
    )

    assert report["queries"] == len(evaluation.reference.queries) >= 30
    assert set(report["metrics"]) >= {
        "recall_at_1",
        "recall_at_5",
        "mrr",
        "unit_integrity",
        "cross_language_recall_at_5",
        "recall_at_5_with_filters",
    }
    assert len(report["per_query"]) == report["queries"]


# --- métriques avec seuils ---------------------------------------------------


async def test_recall_at_5_meets_the_regression_threshold(
    evaluation: Evaluation,
) -> None:
    assert_meets("recall_at_5", evaluation.metrics()["recall_at_5"])


async def test_mrr_meets_the_regression_threshold(evaluation: Evaluation) -> None:
    assert_meets("mrr", evaluation.metrics()["mrr"])


async def test_unit_integrity_meets_the_regression_threshold(
    evaluation: Evaluation,
) -> None:
    assert_meets("unit_integrity", evaluation.metrics()["unit_integrity"])


async def test_cross_language_recall_at_5_meets_the_regression_threshold(
    evaluation: Evaluation,
) -> None:
    assert_meets(
        "cross_language_recall_at_5", evaluation.metrics()["cross_language_recall_at_5"]
    )


async def test_recall_at_5_with_filters_meets_the_regression_threshold(
    evaluation: Evaluation,
) -> None:
    assert_meets(
        "recall_at_5_with_filters", evaluation.metrics()["recall_at_5_with_filters"]
    )


# --- invariants sans seuil ---------------------------------------------------


async def test_filtering_on_the_expected_document_never_lowers_recall(
    evaluation: Evaluation,
) -> None:
    """Chaque filtre est satisfait par le document attendu (vérifié par
    ``test_retrieval_reference_set``) : il ne peut qu'écarter des concurrents."""
    measures = evaluation.metrics()
    with_filters = measures["recall_at_5_with_filters"]
    without_filters = measures["recall_at_5_without_filters_same_queries"]

    assert with_filters is not None
    assert without_filters is not None
    assert with_filters >= without_filters


async def test_filtered_searches_only_return_documents_matching_the_filters(
    evaluation: Evaluation,
) -> None:
    documents = {d.filename: d for d in evaluation.reference.documents}
    for outcome in evaluation.outcomes:
        filters = outcome.query.filters
        if filters is None or outcome.filtered is None:
            continue
        for passage in outcome.filtered.passages:
            document = documents[passage.citation.source_filename]
            assert filters.language in (None, document.language), outcome.query.id
            assert set(filters.topics) <= set(document.topics), outcome.query.id
            assert not filters.kinds or document.kind in filters.kinds, outcome.query.id


async def test_every_returned_passage_carries_a_complete_citation(
    evaluation: Evaluation,
) -> None:
    documents = {d.filename: d for d in evaluation.reference.documents}
    for outcome in evaluation.outcomes:
        for passage in outcome.plain.passages:
            citation = passage.citation
            document = documents[citation.source_filename]
            where = f"{outcome.query.id}/{citation.source_filename}"
            assert citation.document_title == document.title, where
            assert citation.version_number == 1, where
            assert citation.document_id and citation.version_id, where
            assert all(part.strip() for part in citation.section_path), where
            assert citation.source_filename == document.filename, where
            assert passage.matched_chunk_ids, where
            assert passage.text.strip(), where


async def test_a_complete_unit_always_holds_its_question_answer_and_code(
    evaluation: Evaluation,
) -> None:
    """Intégrité d'unité sans seuil : quand le passage attendu est rendu en
    entier (``truncated=False``), il contient forcément tout ce qui appartient
    à son unité."""
    broken = [
        o.query.id
        for o in evaluation.outcomes
        if o.expected_text is not None
        and not o.plain.passages[(o.plain_rank or 1) - 1].truncated
        and not unit_is_intact(o.expected_text, o.query.expected)
    ]

    assert broken == []


async def test_passages_are_ordered_by_decreasing_score_and_distinct_by_unit(
    evaluation: Evaluation,
) -> None:
    for outcome in evaluation.outcomes:
        scores = [p.score for p in outcome.plain.passages]
        units = [p.unit_id for p in outcome.plain.passages]
        assert scores == sorted(scores, reverse=True), outcome.query.id
        assert len(set(units)) == len(units), outcome.query.id
        assert len(units) <= RESULT_LIMIT
