"""Évaluation S5 étendue aux nouveaux formats (S6 ; roadmap : « évaluation S5
étendue aux nouveaux formats », critère « aucune régression des métriques
S5 »).

Le jeu de référence de S5 est rejoué À L'IDENTIQUE (mêmes requêtes, mêmes
extraits attendus, mêmes filtres, mêmes seuils de
``retrieval_thresholds.json``, même pipeline de production : vrai Docling, vrai
E5, Qdrant, PostgreSQL), avec deux documents importés dans un autre format :

- ``retrieval/qa_pytest_fr.md``  -> ``office/qa_pytest_fr.pdf`` (PDF natif) ;
- ``retrieval/cours_git_fr.md``  -> ``office/cours_git_fr.docx`` (DOCX).

Seul le NOM de fichier attendu change (la citation porte le nom du fichier
importé) ; les ``answer_snippet``, ``question`` et ``code_snippet`` attendus
sont ceux du Markdown : le contenu des unités doit donc rester le même. Les
fixtures PDF/DOCX viennent de ``generate_office_fixtures.py``.

Les seuils ne sont PAS recalculés ici : ceux de S5 s'appliquent tels quels. La
mesure de ce jeu est écrite dans ``$KB_RETRIEVAL_REPORT_DIR/
retrieval_report_office.json`` ; ``retrieval_report.json`` (la mesure de
référence de S5) n'est jamais écrasé.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import replace
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
from knowledge_base.domain.value_objects.embedding_profile import MULTILINGUAL_E5_BASE
from knowledge_base.frameworks.configuration.chunking import DEFAULT_CHUNKING_POLICY
from knowledge_base.use_cases.provision_search_index.provision_search_index_command import (  # noqa: E501
    ProvisionSearchIndex,
)
from runtime.database import SessionFactory, create_engine, create_session_factory
from runtime.settings import Settings
from tests.integration.knowledge_base.extraction_support import Imported
from tests.integration.knowledge_base.indexing_support import (
    IndexingRig,
    open_indexing_rig,
)
from tests.integration.knowledge_base.retrieval_support import (
    Reference,
    load_reference,
    recall_at_k,
    unit_is_intact,
)
from tests.integration.knowledge_base.search_support import models_cache
from tests.integration.knowledge_base.support import kb_settings
from tests.integration.knowledge_base.test_retrieval_evaluation import (
    FLOORS,
    RESULT_LIMIT,
    Evaluation,
    assert_meets,
    report_dir,
    search_all,
    threshold_of,
)
from tests.office_support import DOCX, PDF

pytestmark = [
    pytest.mark.integration,
    pytest.mark.model,
    pytest.mark.retrieval,
    pytest.mark.asyncio(loop_scope="module"),
]

# fichier du jeu de S5 -> (fichier de substitution, type de média)
SUBSTITUTES = {
    "retrieval/qa_pytest_fr.md": ("office/qa_pytest_fr.pdf", PDF),
    "retrieval/cours_git_fr.md": ("office/cours_git_fr.docx", DOCX),
}
MEDIA_TYPES = {".pdf": PDF, ".docx": DOCX}
MARKDOWN = "text/markdown"


def make_search(
    session_factory: SessionFactory,
    rig: IndexingRig,
    embedder: SentenceTransformersEmbedder,
) -> KnowledgeSearch:
    """Composition de la recherche, identique à celle de
    ``test_retrieval_evaluation`` : point UNIQUE à adapter si la signature de
    ``compose_knowledge_search`` change."""
    return compose_knowledge_search(
        session_factory,
        rig.qdrant,
        kb_settings(),
        embedder=embedder,
        token_counter=embedder.token_counter,
    )


def office_reference(reference: Reference) -> Reference:
    """Le jeu de S5 avec deux documents (et leurs attendus) en PDF et DOCX."""

    def substitute(file: str) -> str:
        return SUBSTITUTES[file][0] if file in SUBSTITUTES else file

    return Reference(
        documents=tuple(
            replace(d, file=substitute(d.file)) for d in reference.documents
        ),
        queries=tuple(
            replace(q, expected=replace(q.expected, file=substitute(q.expected.file)))
            for q in reference.queries
        ),
    )


async def build_office_corpus(
    rig: IndexingRig, reference: Reference
) -> tuple[UUID, dict[str, Imported]]:
    """Comme ``build_corpus`` de S5, avec le type de média de chaque fichier."""
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
            media_type=MEDIA_TYPES.get(Path(document.file).suffix, MARKDOWN),
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
        assert await rig.extraction.run(found.item_id, extract) == "succeeded", (
            document.file
        )
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
    reference = office_reference(load_reference())
    embedder = SentenceTransformersEmbedder.load(
        MULTILINGUAL_E5_BASE, cache_dir=models_cache()
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("KB_MODELS_CACHE_DIR", models_cache())
        async with open_indexing_rig(
            settings,
            session_factory,
            tmp_path_factory.mktemp("retrieval-office"),
            patch,
            embedder,
        ) as rig:
            index_id, imported = await build_office_corpus(rig, reference)
            search = make_search(session_factory, rig, embedder)
            outcomes = await search_all(search, reference)
            evaluation = Evaluation(reference, outcomes, imported, rig, index_id)
            record(evaluation)
            yield evaluation
    await engine.dispose()


def record(evaluation: Evaluation) -> Path:
    directory = report_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "retrieval_report_office.json"
    path.write_text(
        json.dumps(
            {
                "pipeline": {
                    "profile": MULTILINGUAL_E5_BASE.model,
                    "revision": MULTILINGUAL_E5_BASE.revision,
                    "chunking_policy": DEFAULT_CHUNKING_POLICY.identity,
                    "result_limit": RESULT_LIMIT,
                },
                "substituted_documents": {
                    source: target for source, (target, _) in SUBSTITUTES.items()
                },
                "queries": len(evaluation.reference.queries),
                "metrics": evaluation.metrics(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def converted_queries(evaluation: Evaluation) -> list[int | None]:
    targets = {target for target, _ in SUBSTITUTES.values()}
    return [
        o.plain_rank for o in evaluation.outcomes if o.query.expected.file in targets
    ]


# --- le corpus mixte est en place ----------------------------------------------


async def test_the_pdf_and_the_docx_are_published_with_their_own_media_type(
    evaluation: Evaluation,
) -> None:
    rig = evaluation.rig
    for target, media_type in SUBSTITUTES.values():
        version = await rig.extraction.version(evaluation.imported[target].version_id)
        assert version["status"] == "PUBLISHED", target
        assert version["media_type"] == media_type, target
    assert len(evaluation.imported) == len(evaluation.reference.documents) == 12


async def test_the_office_measure_is_recorded_without_touching_the_s5_reference(
    evaluation: Evaluation,
) -> None:
    report = json.loads(
        (report_dir() / "retrieval_report_office.json").read_text(encoding="utf-8")
    )

    assert report["substituted_documents"] == {
        source: target for source, (target, _) in SUBSTITUTES.items()
    }
    assert report["queries"] == len(evaluation.reference.queries)
    assert set(report["metrics"]) >= set(FLOORS)


# --- aucune régression des seuils de S5 -------------------------------------------


@pytest.mark.parametrize("metric", sorted(FLOORS))
async def test_the_s5_metrics_still_meet_their_thresholds_with_pdf_and_docx(
    evaluation: Evaluation, metric: str
) -> None:
    assert_meets(metric, evaluation.metrics()[metric])


# --- les documents convertis, eux-mêmes ----------------------------------------------


async def test_queries_on_the_converted_documents_meet_the_global_recall_threshold(
    evaluation: Evaluation,
) -> None:
    ranks = converted_queries(evaluation)
    minimum = threshold_of("recall_at_5")

    assert len(ranks) == 8  # fr09-fr13, y01-y03 du jeu de référence
    assert minimum is not None
    assert recall_at_k(ranks, 5) >= minimum, ranks


async def test_a_retrieved_converted_unit_is_intact_with_its_question_answer_and_code(
    evaluation: Evaluation,
) -> None:
    targets = {target for target, _ in SUBSTITUTES.values()}
    retrieved = [
        o
        for o in evaluation.outcomes
        if o.query.expected.file in targets and o.expected_text is not None
    ]

    assert retrieved
    assert [
        o.query.id
        for o in retrieved
        if not unit_is_intact(o.expected_text or "", o.query.expected)
    ] == []


async def test_citations_of_converted_documents_name_the_original_and_carry_its_pages(
    evaluation: Evaluation,
) -> None:
    seen: dict[str, set[tuple[int | None, int | None]]] = {}
    for outcome in evaluation.outcomes:
        for passage in outcome.plain.passages:
            citation = passage.citation
            if citation.source_filename in {"qa_pytest_fr.pdf", "cours_git_fr.docx"}:
                seen.setdefault(citation.source_filename, set()).add(
                    (citation.page_start, citation.page_end)
                )

    assert set(seen) == {"qa_pytest_fr.pdf", "cours_git_fr.docx"}
    # PDF : pages connues, 1 <= début <= fin <= 2 ; DOCX : pas de pagination.
    assert all(
        start is not None and end is not None and 1 <= start <= end <= 2
        for start, end in seen["qa_pytest_fr.pdf"]
    )
    assert seen["cours_git_fr.docx"] == {(None, None)}
