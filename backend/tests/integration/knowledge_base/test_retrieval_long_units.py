"""Unités longues et passages tronqués (S6 ; roadmap S6, critère « intégrité
d'unité mesurée sur au moins une unité tronquée » ; knowledge-base.md §12
étape 6, O12 ``max_passage_tokens``).

Le jeu de S5 n'a que des unités d'un chunk : l'intégrité d'unité n'y est pas
exercée. Ce module ajoute, HORS du jeu de S5 (dont la non-régression reste
mesurée à l'identique par ``test_retrieval_evaluation_office``), le cours PDF
``office/cours_concurrence_long_fr.pdf`` : sa section « Exécuter du code
bloquant depuis une application asyncio » dépasse le budget d'un chunk et
``max_passage_tokens`` (1024). Quatre documents Markdown du corpus de S3/S5
traitent des mêmes notions (négatifs proches) ; plusieurs requêtes nomment un
identifiant de code exact. Jeu : ``office/long_units_reference.json``.

Pipeline de production (vrai Docling PDF, vrai E5, Qdrant, PostgreSQL). Le
corpus est indexé une fois ; les requêtes sont rejouées pour plusieurs budgets
de passage (``BUDGETS``) : c'est la remesure d'O12. Mesure écrite dans
``$KB_RETRIEVAL_REPORT_DIR/retrieval_report_long_units.json``.

Une unité longue est reconnue à sa PROVENANCE (fichier et section de la
citation), pas à l'extrait attendu : une fenêtre tronquée qui a perdu la
réponse est un défaut d'intégrité, pas une unité « non retrouvée ». Seuils :
``office/long_units_thresholds.json`` (première mesure moins une marge, même
règle que S5) ; ceux de S5 restent appliqués au jeu de S5 par
``test_retrieval_evaluation_office``.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest
import pytest_asyncio

from knowledge_base.adapters.controller.composition import compose_knowledge_search
from knowledge_base.adapters.gateway.embedding.sentence_transformers_embedder import (
    SentenceTransformersEmbedder,
)
from knowledge_base.contracts.result import RetrievedPassage
from knowledge_base.domain.value_objects.embedding_profile import MULTILINGUAL_E5_BASE
from knowledge_base.frameworks.configuration.chunking import DEFAULT_CHUNKING_POLICY
from runtime.database import create_engine, create_session_factory
from runtime.settings import Settings
from tests.integration.knowledge_base.extraction_support import Imported
from tests.integration.knowledge_base.indexing_support import (
    IndexingRig,
    open_indexing_rig,
)
from tests.integration.knowledge_base.retrieval_support import (
    Reference,
    load_reference,
    mean_reciprocal_rank,
    recall_at_k,
    unit_integrity,
    unit_is_intact,
)
from tests.integration.knowledge_base.search_support import models_cache
from tests.integration.knowledge_base.support import kb_settings
from tests.integration.knowledge_base.test_retrieval_evaluation import (
    QueryOutcome,
    report_dir,
    search_all,
)
from tests.integration.knowledge_base.test_retrieval_evaluation_office import (
    build_office_corpus,
)
from tests.office_support import OFFICE
from tests.unit.knowledge_base.use_cases.indexing_fakes import single_profile

pytestmark = [
    pytest.mark.integration,
    pytest.mark.model,
    pytest.mark.retrieval,
    pytest.mark.asyncio(loop_scope="module"),
]

LONG_UNITS = OFFICE / "long_units_reference.json"
LONG_THRESHOLDS = OFFICE / "long_units_thresholds.json"
# Métriques soumises à un seuil, au budget par défaut.
LONG_METRICS = ("long_unit_found_at_5", "recall_at_5", "truncated_unit_integrity")
LONG_FILE = "office/cours_concurrence_long_fr.pdf"
LONG_SECTION = "Exécuter du code bloquant depuis une application asyncio"
# Budgets de passage rejoués : un chunk entier (minimum admis), le défaut
# (1024) et au-delà, jusqu'à contenir l'unité longue entière.
BUDGETS = (508, 768, 1024, 1536, 2048)
DEFAULT_BUDGET = 1024
TOP = 5


def long_unit_passage(outcome: QueryOutcome) -> RetrievedPassage | None:
    """Passage de la section longue dans les 5 premiers, identifié par sa
    PROVENANCE (fichier et section de la citation), pas par l'extrait
    attendu : une fenêtre qui a perdu la réponse reste le passage de l'unité
    attendue, et c'est précisément ce que l'intégrité doit compter."""
    for passage in outcome.plain.passages[:TOP]:
        citation = passage.citation
        if (
            citation.source_filename == outcome.query.expected.filename
            and citation.section_path[-1:] == (LONG_SECTION,)
        ):
            return passage
    return None


@dataclass(frozen=True)
class BudgetRun:
    budget: int
    outcomes: tuple[QueryOutcome, ...]
    # Tokens (tokenizer du profil) du passage attendu, s'il est dans le top 5.
    expected_tokens: Mapping[str, int]
    # Tokens des 5 premiers passages réunis, par requête.
    top_tokens: Mapping[str, int]

    def long_unit_outcomes(self) -> list[QueryOutcome]:
        return [o for o in self.outcomes if "long_unit" in o.query.tags]

    def truncated_long_passages(self) -> list[tuple[QueryOutcome, RetrievedPassage]]:
        found = [(o, long_unit_passage(o)) for o in self.long_unit_outcomes()]
        return [(o, p) for o, p in found if p is not None and p.truncated]

    def metrics(self) -> dict[str, float | int | None]:
        ranks = [o.plain_rank for o in self.outcomes]
        long_units = self.long_unit_outcomes()
        truncated = self.truncated_long_passages()
        return {
            # Définitions de S5 (unité reconnue à sa réponse de référence).
            "recall_at_5": recall_at_k(ranks, TOP),
            "mrr": mean_reciprocal_rank(ranks),
            "unit_integrity": unit_integrity(
                [(o.query.expected, o.expected_text) for o in self.outcomes]
            ),
            # Unité longue reconnue à sa provenance.
            "long_unit_found_at_5": sum(
                1 for o in long_units if long_unit_passage(o) is not None
            )
            / len(long_units),
            "truncated_unit_integrity": unit_integrity(
                [(o.query.expected, p.text) for o, p in truncated]
            ),
            "truncated_long_unit_passages": len(truncated),
            "expected_passage_tokens_max": max(self.expected_tokens.values()),
            "expected_passage_tokens_median": statistics.median(
                self.expected_tokens.values()
            ),
            "top_5_tokens_max": max(self.top_tokens.values()),
            "top_5_tokens_median": statistics.median(self.top_tokens.values()),
        }


@dataclass(frozen=True)
class LongUnits:
    reference: Reference
    imported: dict[str, Imported]
    rig: IndexingRig
    index_id: UUID
    runs: Mapping[int, BudgetRun]

    @property
    def default(self) -> BudgetRun:
        return self.runs[DEFAULT_BUDGET]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def long_units(
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncIterator[LongUnits]:
    settings = Settings(environment="test", external_timeout_seconds=5.0)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    reference = load_reference(LONG_UNITS)
    embedder = SentenceTransformersEmbedder.load(
        MULTILINGUAL_E5_BASE, cache_dir=models_cache()
    )
    counter = embedder.token_counter
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("KB_MODELS_CACHE_DIR", models_cache())
        async with open_indexing_rig(
            settings,
            session_factory,
            tmp_path_factory.mktemp("retrieval-long-units"),
            patch,
            embedder,
        ) as rig:
            index_id, imported = await build_office_corpus(rig, reference)
            runs: dict[int, BudgetRun] = {}
            for budget in BUDGETS:
                search = compose_knowledge_search(
                    session_factory,
                    rig.qdrant,
                    kb_settings(max_passage_tokens=budget),
                    embedders=single_profile(embedder, counter),
                )
                outcomes = await search_all(search, reference)
                runs[budget] = BudgetRun(
                    budget=budget,
                    outcomes=outcomes,
                    expected_tokens={
                        o.query.id: counter.count(o.expected_text)
                        for o in outcomes
                        if o.expected_text is not None
                    },
                    top_tokens={
                        o.query.id: sum(
                            counter.count(p.text) for p in o.plain.passages[:TOP]
                        )
                        for o in outcomes
                    },
                )
            measured = LongUnits(reference, imported, rig, index_id, runs)
            record(measured)
            yield measured
    await engine.dispose()


def per_query(outcome: QueryOutcome, run: BudgetRun) -> dict[str, object]:
    passage = long_unit_passage(outcome) if "long_unit" in outcome.query.tags else None
    return {
        "id": outcome.query.id,
        "tags": list(outcome.query.tags),
        "rank": outcome.plain_rank,
        "long_unit_in_top_5": passage is not None,
        "truncated": None if passage is None else passage.truncated,
        "intact": (
            None
            if passage is None
            else unit_is_intact(passage.text, outcome.query.expected)
        ),
        "matched_chunks": None if passage is None else len(passage.matched_chunk_ids),
        "passage_tokens": run.expected_tokens.get(outcome.query.id),
    }


def record(measured: LongUnits) -> Path:
    directory = report_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "retrieval_report_long_units.json"
    path.write_text(
        json.dumps(
            {
                "pipeline": {
                    "profile": MULTILINGUAL_E5_BASE.model,
                    "revision": MULTILINGUAL_E5_BASE.revision,
                    "chunking_policy": DEFAULT_CHUNKING_POLICY.identity,
                    "chunk_max_tokens": DEFAULT_CHUNKING_POLICY.max_tokens,
                },
                "documents": [d.file for d in measured.reference.documents],
                "queries": len(measured.reference.queries),
                "by_budget": {
                    str(budget): {
                        "metrics": run.metrics(),
                        "per_query": [per_query(o, run) for o in run.outcomes],
                    }
                    for budget, run in measured.runs.items()
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


# --- le cas long existe bien -----------------------------------------------------


async def test_the_long_section_is_one_multi_chunk_unit_over_two_pages(
    long_units: LongUnits,
) -> None:
    version_id = long_units.imported[LONG_FILE].version_id
    extraction = long_units.rig.extraction
    (unit,) = [
        u
        for u in await extraction.units(version_id)
        if u["section_path"][-1] == LONG_SECTION
    ]
    chunks = [
        c for c in await extraction.chunks(version_id) if c["unit_id"] == unit["id"]
    ]

    assert unit["token_count"] > DEFAULT_BUDGET
    assert len(chunks) >= 3
    assert (unit["page_start"], unit["page_end"]) == (1, 2)


async def test_the_default_budget_truncates_the_long_unit(
    long_units: LongUnits,
) -> None:
    """Sans passage tronqué, l'intégrité ci-dessous ne mesurerait rien."""
    assert long_units.default.truncated_long_passages()


async def test_a_budget_holding_the_whole_unit_truncates_nothing(
    long_units: LongUnits,
) -> None:
    assert long_units.runs[max(BUDGETS)].truncated_long_passages() == []


# --- non-régression ----------------------------------------------------------------


def long_threshold(metric: str) -> float | None:
    raw = json.loads(LONG_THRESHOLDS.read_text(encoding="utf-8"))
    minimum = raw["metrics"][metric]["minimum"]
    return None if minimum is None else float(minimum)


@pytest.mark.parametrize("metric", sorted(LONG_METRICS))
async def test_the_long_units_metrics_meet_their_thresholds(
    long_units: LongUnits, metric: str
) -> None:
    """Seuils propres à ce jeu (``long_units_thresholds.json``), fixés sur la
    première mesure moins une marge, comme ceux de S5 ; tant qu'un minimum vaut
    ``null``, le test échoue en affichant la valeur mesurée."""
    measured = long_units.default.metrics()[metric]
    minimum = long_threshold(metric)

    assert measured is not None
    assert minimum is not None, f"{metric} : seuil non fixé, mesuré {measured}"
    assert measured >= minimum, (metric, measured, minimum)


async def test_the_long_units_measure_is_recorded(long_units: LongUnits) -> None:
    report = json.loads(
        (report_dir() / "retrieval_report_long_units.json").read_text(encoding="utf-8")
    )

    assert set(report["by_budget"]) == {str(b) for b in BUDGETS}
    assert report["queries"] == len(long_units.reference.queries)
