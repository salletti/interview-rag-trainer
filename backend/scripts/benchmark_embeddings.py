"""Benchmark des profils d'embeddings (S9 ; roadmap S9, ADR 0011 §6,
knowledge-base.md §16 O1, O4, O6, O12).

Dépendance assumée au code de test (revue S9) : le jeu de référence, son
chargement et les métriques (Recall@k, MRR, intégrité d'unité) sont importés
de ``tests/integration/knowledge_base/retrieval_support``, pour que le
benchmark et l'évaluation de S5/S6 mesurent avec les MÊMES définitions, jamais
recopiées. Le script ne s'exécute donc que depuis un checkout complet de
``backend/`` (``make benchmark-embeddings``), pas comme un module de
production ; extraire ces métriques dans un module commun n'aurait pas de
second usage hors des tests.

Usage (depuis ``backend/``, ou ``make benchmark-embeddings``) ::

    uv run python scripts/benchmark_embeddings.py [nom ...]

Sans nom, tous les candidats de ``CANDIDATES``. Chaque candidat est mesuré
dans un PROCESSUS NEUF (``--one``) : la RSS d'un modèle n'est pas faussée par
celui d'avant. Rapport JSON : ``.cache/benchmark/embedding_benchmark.json``
(ignoré par git) ; tableau Markdown sur la sortie standard.

Pipeline : celui de la production jusqu'à l'encodage, en mémoire ensuite.

- Documents : jeux de S5 (``retrieval/retrieval_reference.json``, Markdown),
  de S6 (même jeu, deux documents en PDF et DOCX ; unités longues :
  ``office/long_units_reference.json``) et complément S9
  (``retrieval/benchmark_s9_queries.json`` : identifiants de code, négatifs
  proches). Parsing par les parseurs de production (Docling), segmentation et
  découpage par ``VersionExtractor`` avec la politique du candidat (budget
  508 tokens comptés par SON tokenizer), encodage par
  ``SentenceTransformersEmbedder`` (préfixes et normalisation du profil).
- Recherche : similarité exacte (produit scalaire de vecteurs normalisés,
  ``query_points_groups`` de Qdrant fait de même sous son seuil d'indexation
  HNSW, 10 000 points par défaut) ; regroupement par unité (3 meilleurs chunks
  au plus, score = meilleur chunk) ; filtres sur les métadonnées du document ;
  10 passages reconstruits par ``assemble_passage`` (``max_passage_tokens`` =
  1024, tokenizer du candidat). Tous les documents sont publiés : le contrôle
  SQL n'écarte rien, comme dans les mesures de S5.
- Métriques : définitions de ``tests/integration/knowledge_base/
  retrieval_support`` (Recall@k, MRR, intégrité d'unité), unité longue
  reconnue à sa provenance (S6).
- Recherche hybride (O6) : BM25 sur le texte des chunks (identifiants de code
  gardés entiers), fusionné au classement dense par Reciprocal Rank Fusion
  (k = 60) au niveau des unités ; mesurée, jamais utilisée par la production.
- Ressources : RSS du processus (``ps -o rss``) après import de la pile, après
  chargement, après encodage du corpus ; pic (``ru_maxrss``) ; durée de
  chargement (cache chaud) ; encodage des passages par lots de 32 (médiane
  par chunk) ; encodage d'une requête (médiane, p95, hors première) ; taille
  des poids sur disque.
"""

from __future__ import annotations

import argparse
import asyncio
import copyreg
import hashlib
import json
import math
import os
import pickle
import platform
import re
import resource
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final
from uuid import NAMESPACE_URL, UUID, uuid5

import numpy as np

BACKEND: Final = Path(__file__).resolve().parents[1]
# Le jeu de référence et ses métriques sont du code de test (``tests/``) :
# les mêmes définitions que l'évaluation de S5, jamais recopiées.
sys.path.insert(0, str(BACKEND))

from knowledge_base.adapters.controller.tasks.extract_version_handler import (  # noqa: E402
    default_parser,
)
from knowledge_base.adapters.gateway.embedding.sentence_transformers_embedder import (  # noqa: E402
    SentenceTransformersEmbedder,
)
from knowledge_base.domain.entities.knowledge_document import (  # noqa: E402
    DocumentVersion,
    KnowledgeDocument,
)
from knowledge_base.domain.services.passage_assembler import (  # noqa: E402
    assemble_passage,
)
from knowledge_base.domain.value_objects.chunk_hit import ChunkHit  # noqa: E402
from knowledge_base.domain.value_objects.chunking_policy import (  # noqa: E402
    ChunkingPolicy,
)
from knowledge_base.domain.value_objects.citation import Citation  # noqa: E402
from knowledge_base.domain.value_objects.content_hash import (  # noqa: E402
    ContentHash,
)
from knowledge_base.domain.value_objects.document_chunk import (  # noqa: E402
    DocumentChunk,
)
from knowledge_base.domain.value_objects.document_kind import (  # noqa: E402
    DocumentKind,
)
from knowledge_base.domain.value_objects.document_source import (  # noqa: E402
    DocumentSource,
)
from knowledge_base.domain.value_objects.embedding_profile import (  # noqa: E402
    MULTILINGUAL_E5_BASE,
    MULTILINGUAL_E5_SMALL,
    Distance,
    EmbeddingProfile,
)
from knowledge_base.domain.value_objects.knowledge_unit import (  # noqa: E402
    KnowledgeUnit,
)
from knowledge_base.domain.value_objects.published_unit import (  # noqa: E402
    PublishedUnit,
    UnitChunk,
)
from knowledge_base.domain.value_objects.structured_document import (  # noqa: E402
    StructuredDocument,
    render_blocks,
)
from knowledge_base.frameworks.configuration.chunking import (  # noqa: E402
    DEFAULT_CHUNKING_POLICY,
)
from knowledge_base.use_cases.extraction_steps import VersionExtractor  # noqa: E402
from tests.integration.knowledge_base.retrieval_support import (  # noqa: E402
    FIXTURES,
    Expected,
    Reference,
    ReferenceQuery,
    load_reference,
    mean_reciprocal_rank,
    rank_of_expected,
    recall_at_k,
    unit_integrity,
    unit_is_intact,
)

MEDIA_TYPES: Final = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
# Substitutions de l'évaluation S6 (``test_retrieval_evaluation_office``).
OFFICE_SUBSTITUTES: Final = {
    "retrieval/qa_pytest_fr.md": "office/qa_pytest_fr.pdf",
    "retrieval/cours_git_fr.md": "office/cours_git_fr.docx",
}
LONG_SECTION: Final = "Exécuter du code bloquant depuis une application asyncio"
RESULT_LIMIT: Final = 10
CHUNKS_PER_UNIT: Final = 3
MAX_PASSAGE_TOKENS: Final = 1024
ENCODE_BATCH: Final = 32
# Budget d'un chunk, identique pour tous les candidats (celui de la production,
# ADR 0009 §3), compté par le tokenizer de chaque candidat.
CHUNK_BUDGET: Final = DEFAULT_CHUNKING_POLICY.max_tokens
RRF_K: Final = 60
HYBRID_DEPTH: Final = 50
REPORT_DIR: Final = BACKEND / ".cache" / "benchmark"
# Documents parsés une fois par le processus parent (Docling) : les modèles de
# layout de Docling PDF ne pèsent pas dans la RSS mesurée d'un candidat.
PARSED: Final = REPORT_DIR / "parsed_documents.pickle"
CACHE_DIR: Final = Path(
    os.environ.get("SENTENCE_TRANSFORMERS_HOME", BACKEND / ".cache" / "models")
)


# --- candidats --------------------------------------------------------------------


BGE_M3_REVISION: Final = "5617a9f61b028005a4858fdac845db406aefb181"
QWEN3_REVISION: Final = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"

BGE_M3: Final = EmbeddingProfile(
    model="BAAI/bge-m3",
    revision=BGE_M3_REVISION,
    dimension=1024,
    distance=Distance.COSINE,
    normalize=True,
    # Vecteur dense de bge-m3 : aucune instruction ni préfixe (carte du modèle).
    query_prefix="",
    passage_prefix="",
    max_tokens=8192,
    tokenizer=f"xlmr-{BGE_M3_REVISION[:8]}",
)
QWEN3_EMBEDDING_06B: Final = EmbeddingProfile(
    model="Qwen/Qwen3-Embedding-0.6B",
    revision=QWEN3_REVISION,
    dimension=1024,
    distance=Distance.COSINE,
    normalize=True,
    # Invite « query » de config_sentence_transformers.json à cette révision ;
    # les passages ne reçoivent aucune instruction.
    query_prefix=(
        "Instruct: Given a web search query, retrieve relevant passages that "
        "answer the query\nQuery:"
    ),
    passage_prefix="",
    max_tokens=32768,
    tokenizer=f"qwen3-{QWEN3_REVISION[:8]}",
)

CANDIDATES: Final[Mapping[str, EmbeddingProfile]] = {
    "multilingual-e5-small": MULTILINGUAL_E5_SMALL,
    "multilingual-e5-base": MULTILINGUAL_E5_BASE,
    "bge-m3": BGE_M3,
    "qwen3-embedding-0.6b": QWEN3_EMBEDDING_06B,
}


def policy_for(profile: EmbeddingProfile) -> ChunkingPolicy:
    return ChunkingPolicy(
        version=DEFAULT_CHUNKING_POLICY.version,
        tokenizer=profile.tokenizer,
        max_tokens=CHUNK_BUDGET,
    )


# --- corpus en mémoire ---------------------------------------------------------------


@dataclass(frozen=True)
class IndexedUnit:
    unit: PublishedUnit
    file: str
    kind: str
    language: str
    topics: tuple[str, ...]


@dataclass
class Corpus:
    """Unités et chunks d'un jeu de documents, vecteurs des chunks."""

    units: dict[UUID, IndexedUnit] = field(default_factory=dict)
    chunks: list[DocumentChunk] = field(default_factory=list)
    vectors: np.ndarray[Any, np.dtype[np.float32]] | None = None

    def matrix(self) -> np.ndarray[Any, np.dtype[np.float32]]:
        assert self.vectors is not None, "corpus non encodé"
        return self.vectors


def document_for(
    file: str, title: str, kind: str, language: str, topics: Sequence[str]
) -> tuple[KnowledgeDocument, DocumentVersion, bytes]:
    path = FIXTURES / file
    content = path.read_bytes()
    document = KnowledgeDocument.import_new(
        document_id=uuid5(NAMESPACE_URL, f"kb-benchmark/{file}"),
        version_id=uuid5(NAMESPACE_URL, f"kb-benchmark/{file}/v1"),
        title=title,
        kind=DocumentKind(kind),
        topics=tuple(topics),
        language=language,
        source=DocumentSource(
            filename=path.name,
            media_type=MEDIA_TYPES[path.suffix],
            size_bytes=len(content),
            content_hash=ContentHash(hashlib.sha256(content).hexdigest()),
            imported_at=datetime(2026, 11, 18, tzinfo=UTC),
        ),
    )
    (version,) = document.versions
    return document, version, content


class ParsedDocuments:
    """``DocumentParser`` qui rend le document parsé par le parent, par nom
    de fichier ; même résultat que ``default_parser()`` (déterministe)."""

    def __init__(self, parsed: Mapping[str, StructuredDocument]) -> None:
        self._parsed = parsed

    def parse(
        self, content: bytes, media_type: str, filename: str
    ) -> StructuredDocument:
        return self._parsed[filename]


def benchmark_references() -> dict[str, Reference]:
    s5 = load_reference()
    return {
        "s5": s5,
        "s6_office": office_reference(s5),
        "s6_long_units": load_reference(
            FIXTURES / "office" / "long_units_reference.json"
        ),
        "s9_supplement": load_reference(
            FIXTURES / "retrieval" / "benchmark_s9_queries.json"
        ),
    }


def frozen_mapping(items: dict[Any, Any]) -> MappingProxyType[Any, Any]:
    """Reconstruit un ``MappingProxyType`` au chargement du pickle (le parent
    et l'enfant exécutent ce même module comme ``__main__``)."""
    return MappingProxyType(items)


def parse_all() -> None:
    """Parse chaque fichier des jeux une fois, avec les parseurs de
    production, et les range dans ``PARSED``."""
    parser = default_parser()
    parsed: dict[str, StructuredDocument] = {}
    for reference in benchmark_references().values():
        for entry in reference.documents:
            path = FIXTURES / entry.file
            if path.name not in parsed:
                parsed[path.name] = parser.parse(
                    path.read_bytes(), MEDIA_TYPES[path.suffix], path.name
                )
    # Les métadonnées des blocs sont des ``MappingProxyType`` (immuables), que
    # pickle ne sait pas sérialiser seul.
    copyreg.pickle(MappingProxyType, lambda m: (frozen_mapping, (dict(m),)))
    PARSED.write_bytes(pickle.dumps(parsed))


def build_corpus(reference: Reference, extractor: VersionExtractor) -> Corpus:
    corpus = Corpus()
    for entry in reference.documents:
        document, version, content = document_for(
            entry.file, entry.title, entry.kind, entry.language, entry.topics
        )
        units, chunks = extractor.extract(content, document, version)
        by_unit: defaultdict[UUID, list[DocumentChunk]] = defaultdict(list)
        for chunk in chunks:
            by_unit[chunk.unit_id].append(chunk)
        for unit in units:
            corpus.units[unit.unit_id] = IndexedUnit(
                unit=published_unit(document, version, unit, by_unit[unit.unit_id]),
                file=entry.file,
                kind=entry.kind,
                language=entry.language,
                topics=entry.topics,
            )
        corpus.chunks.extend(chunks)
    return corpus


def published_unit(
    document: KnowledgeDocument,
    version: DocumentVersion,
    unit: KnowledgeUnit,
    chunks: Sequence[DocumentChunk],
) -> PublishedUnit:
    """Ce que ``SqlAlchemyPublishedUnitReader`` relirait de PostgreSQL."""
    return PublishedUnit(
        unit_id=unit.unit_id,
        unit_type=unit.unit_type,
        question_text=unit.question_text,
        text=render_blocks(unit.blocks),
        blocks_token_count=unit.token_count,
        chunks=tuple(
            UnitChunk(
                chunk_id=c.chunk_id,
                ordinal_in_unit=c.ordinal_in_unit,
                text=c.contextual_text,
                token_count=c.token_count,
            )
            for c in sorted(chunks, key=lambda c: c.ordinal_in_unit)
        ),
        citation=Citation(
            document_id=document.document_id,
            document_title=document.title,
            version_id=version.version_id,
            version_number=version.number,
            section_path=unit.section_path,
            page_start=unit.page_span.start,
            page_end=unit.page_span.end,
            source_filename=version.source.filename,
        ),
    )


# --- recherche ----------------------------------------------------------------------


def passes_filters(unit: IndexedUnit, query: ReferenceQuery, filtered: bool) -> bool:
    if not filtered or query.filters is None:
        return True
    filters = query.filters
    return (
        (filters.language is None or unit.language == filters.language)
        and set(filters.topics) <= set(unit.topics)
        and (not filters.kinds or unit.kind in filters.kinds)
    )


def dense_unit_ranking(
    corpus: Corpus, scores: np.ndarray[Any, np.dtype[np.float32]]
) -> list[tuple[UUID, list[ChunkHit]]]:
    """Unités par meilleur score de chunk, 3 meilleurs chunks au plus."""
    hits: defaultdict[UUID, list[ChunkHit]] = defaultdict(list)
    for position in np.argsort(-scores):
        chunk = corpus.chunks[int(position)]
        group = hits[chunk.unit_id]
        if len(group) < CHUNKS_PER_UNIT:
            group.append(
                ChunkHit(chunk.chunk_id, chunk.unit_id, float(scores[position]))
            )
    return sorted(hits.items(), key=lambda item: -item[1][0].score)


def passages(
    corpus: Corpus,
    ranking: Sequence[tuple[UUID, list[ChunkHit]]],
    query: ReferenceQuery,
    *,
    filtered: bool,
    token_counter: Any,
) -> list[tuple[IndexedUnit, str, bool]]:
    found: list[tuple[IndexedUnit, str, bool]] = []
    for unit_id, unit_hits in ranking:
        unit = corpus.units[unit_id]
        if not passes_filters(unit, query, filtered):
            continue
        assembled = assemble_passage(
            unit.unit,
            unit_hits,
            max_tokens=MAX_PASSAGE_TOKENS,
            token_counter=token_counter,
        )
        found.append((unit, assembled.text, assembled.truncated))
        if len(found) == RESULT_LIMIT:
            break
    return found


# --- BM25 et fusion (O6) -----------------------------------------------------------


_TOKEN = re.compile(r"[\w@][\w.@/-]*[\w]|[\w]", re.UNICODE)


def lexical_tokens(text: str) -> list[str]:
    """Mots en minuscules ; un identifiant (``asyncio.create_task``,
    ``dependency_overrides``, ``--frozen``) reste un seul terme, et ses parties
    séparées par un point sont aussi indexées."""
    tokens: list[str] = []
    for match in _TOKEN.findall(text.lower()):
        tokens.append(match)
        if "." in match:
            tokens.extend(part for part in match.split(".") if part)
    return tokens


class Bm25:
    def __init__(self, documents: Sequence[str], k1: float = 1.2, b: float = 0.75):
        self._docs = [Counter(lexical_tokens(d)) for d in documents]
        self._lengths = [sum(d.values()) for d in self._docs]
        self._average = statistics.fmean(self._lengths) if self._lengths else 0.0
        frequency: Counter[str] = Counter()
        for counts in self._docs:
            frequency.update(counts.keys())
        total = len(self._docs)
        self._idf = {
            term: math.log(1 + (total - n + 0.5) / (n + 0.5))
            for term, n in frequency.items()
        }
        self._k1, self._b = k1, b

    def scores(self, query: str) -> list[float]:
        terms = lexical_tokens(query)
        result: list[float] = []
        for counts, length in zip(self._docs, self._lengths, strict=True):
            score = 0.0
            for term in terms:
                tf = counts.get(term, 0)
                if not tf:
                    continue
                norm = self._k1 * (1 - self._b + self._b * length / self._average)
                score += self._idf[term] * tf * (self._k1 + 1) / (tf + norm)
            result.append(score)
        return result


def hybrid_unit_ranking(
    corpus: Corpus,
    dense: Sequence[tuple[UUID, list[ChunkHit]]],
    lexical: Sequence[float],
) -> list[tuple[UUID, list[ChunkHit]]]:
    """RRF sur les rangs d'unités dense et BM25 (``HYBRID_DEPTH`` premiers de
    chacun) ; les chunks gardés pour la reconstruction sont ceux du dense."""
    lexical_best: dict[UUID, float] = {}
    for chunk, score in zip(corpus.chunks, lexical, strict=True):
        if score > lexical_best.get(chunk.unit_id, 0.0):
            lexical_best[chunk.unit_id] = score
    lexical_rank = sorted(lexical_best, key=lambda u: -lexical_best[u])
    fused: defaultdict[UUID, float] = defaultdict(float)
    for rank, (unit_id, _) in enumerate(dense[:HYBRID_DEPTH], start=1):
        fused[unit_id] += 1 / (RRF_K + rank)
    for rank, unit_id in enumerate(lexical_rank[:HYBRID_DEPTH], start=1):
        fused[unit_id] += 1 / (RRF_K + rank)
    by_unit = dict(dense)
    return [
        (unit_id, by_unit[unit_id])
        for unit_id in sorted(fused, key=lambda u: -fused[u])
        if unit_id in by_unit
    ]


# --- mesure d'un candidat -----------------------------------------------------------


def rss_mib() -> float:
    """RSS courante du processus, lue par ``ps`` (macOS et Linux) : aucune
    dépendance ajoutée au projet pour une mesure."""
    found = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(os.getpid())],
        capture_output=True,
        text=True,
        check=True,
    )
    return round(int(found.stdout.strip()) / 2**10, 1)


def peak_rss_mib() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Octets sous macOS, Kio sous Linux.
    divisor = 2**20 if sys.platform == "darwin" else 2**10
    return round(peak / divisor, 1)


def weights_mib(profile: EmbeddingProfile) -> float:
    folder = CACHE_DIR / f"models--{profile.model.replace('/', '--')}"
    snapshot = folder / "snapshots" / profile.revision
    total = sum(
        path.stat().st_size
        for path in snapshot.rglob("*")
        if path.is_file() and path.suffix in {".safetensors", ".bin"}
    )
    return round(total / 2**20, 1)


def percentile(values: Sequence[float], p: float) -> float:
    """Percentile « au rang supérieur » (méthode nearest-rank)."""
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(p * len(ordered)) - 1)
    return float(ordered[max(index, 0)])


@dataclass(frozen=True)
class QuerySet:
    name: str
    reference: Reference
    corpus: Corpus


def office_reference(reference: Reference) -> Reference:
    def substitute(file: str) -> str:
        return OFFICE_SUBSTITUTES.get(file, file)

    return Reference(
        documents=tuple(
            replace(d, file=substitute(d.file)) for d in reference.documents
        ),
        queries=tuple(
            replace(q, expected=replace(q.expected, file=substitute(q.expected.file)))
            for q in reference.queries
        ),
    )


def long_unit_rank(
    found: Sequence[tuple[IndexedUnit, str, bool]], expected: Expected
) -> tuple[int | None, str | None, bool]:
    for position, (unit, text, truncated) in enumerate(found, start=1):
        citation = unit.unit.citation
        if citation.source_filename == expected.filename and (
            citation.section_path.parts[-1:] == (LONG_SECTION,)
        ):
            return position, text, truncated
    return None, None, False


def measure(name: str) -> dict[str, Any]:
    profile = CANDIDATES[name]
    import sentence_transformers  # noqa: F401 - pile importée avant la mesure de base

    rss_stack = rss_mib()
    started = time.perf_counter()
    embedder = SentenceTransformersEmbedder.load(profile, cache_dir=CACHE_DIR)
    load_s = time.perf_counter() - started
    rss_loaded = rss_mib()
    counter = embedder.token_counter
    passage_prefix_tokens = counter.count(profile.passage_prefix)
    parsed: dict[str, StructuredDocument] = pickle.loads(PARSED.read_bytes())
    extractor = VersionExtractor(
        parser=ParsedDocuments(parsed),
        token_counter=counter,
        policy=policy_for(profile),
        max_document_tokens=None,
    )
    references = benchmark_references()
    s5_corpus = build_corpus(references["s5"], extractor)
    sets = [
        QuerySet("s5", references["s5"], s5_corpus),
        QuerySet(
            "s6_office",
            references["s6_office"],
            build_corpus(references["s6_office"], extractor),
        ),
        QuerySet(
            "s6_long_units",
            references["s6_long_units"],
            build_corpus(references["s6_long_units"], extractor),
        ),
        # Même corpus que S5 : les requêtes complémentaires le visent.
        QuerySet("s9_supplement", references["s9_supplement"], s5_corpus),
    ]

    # Encodage : chaque chunk distinct une fois, par lots de 32.
    distinct: dict[UUID, DocumentChunk] = {}
    for query_set in sets:
        for chunk in query_set.corpus.chunks:
            distinct.setdefault(chunk.chunk_id, chunk)
    chunk_list = list(distinct.values())
    vectors: dict[UUID, list[float]] = {}
    per_chunk_s: list[float] = []
    for start in range(0, len(chunk_list), ENCODE_BATCH):
        batch = chunk_list[start : start + ENCODE_BATCH]
        began = time.perf_counter()
        encoded = asyncio.run(
            embedder.embed_passages([c.contextual_text for c in batch])
        )
        per_chunk_s.append((time.perf_counter() - began) / len(batch))
        for chunk, vector in zip(batch, encoded, strict=True):
            vectors[chunk.chunk_id] = vector
    for query_set in sets:
        query_set.corpus.vectors = np.array(
            [vectors[c.chunk_id] for c in query_set.corpus.chunks], dtype=np.float32
        )
    rss_encoded = rss_mib()

    query_s: list[float] = []
    asyncio.run(embedder.embed_query("échauffement"))
    results: dict[str, Any] = {}
    for query_set in sets:
        results[query_set.name] = evaluate(query_set, embedder, query_s)
    token_counts = [c.token_count for c in chunk_list]
    return {
        "name": name,
        "profile": {
            "model": profile.model,
            "revision": profile.revision,
            "dimension": profile.dimension,
            "max_tokens": profile.max_tokens,
            "query_prefix": profile.query_prefix,
            "passage_prefix": profile.passage_prefix,
            "tokenizer": profile.tokenizer,
        },
        "chunking_policy": policy_for(profile).identity,
        "passage_prefix_tokens": passage_prefix_tokens,
        "resources": {
            "weights_mib": weights_mib(profile),
            "load_seconds": round(load_s, 2),
            "rss_stack_mib": rss_stack,
            "rss_after_load_mib": rss_loaded,
            "rss_after_encoding_mib": rss_encoded,
            "peak_rss_mib": peak_rss_mib(),
            "chunks_encoded": len(chunk_list),
            "chunk_tokens_median": statistics.median(token_counts),
            "chunk_tokens_max": max(token_counts),
            "passage_ms_per_chunk_median": round(
                1000 * statistics.median(per_chunk_s), 1
            ),
            "query_ms_median": round(1000 * statistics.median(query_s), 1),
            "query_ms_p95": round(1000 * percentile(query_s, 0.95), 1),
            "queries_timed": len(query_s),
        },
        "sets": results,
    }


def evaluate(
    query_set: QuerySet, embedder: SentenceTransformersEmbedder, query_s: list[float]
) -> dict[str, Any]:
    corpus = query_set.corpus
    counter = embedder.token_counter
    bm25 = Bm25([c.contextual_text for c in corpus.chunks])
    per_query: list[dict[str, Any]] = []
    for query in query_set.reference.queries:
        began = time.perf_counter()
        vector = asyncio.run(embedder.embed_query(query.text))
        query_s.append(time.perf_counter() - began)
        scores = corpus.matrix() @ np.asarray(vector, dtype=np.float32)
        dense = dense_unit_ranking(corpus, scores)
        hybrid = hybrid_unit_ranking(corpus, dense, bm25.scores(query.text))
        row: dict[str, Any] = {"id": query.id, "tags": list(query.tags)}
        for label, ranking in (("dense", dense), ("hybrid", hybrid)):
            plain = passages(
                corpus, ranking, query, filtered=False, token_counter=counter
            )
            listing = [(u.unit.citation.source_filename, t) for u, t, _ in plain]
            rank = rank_of_expected(listing, query.expected)
            row[f"{label}_rank"] = rank
            row[f"{label}_expected_text"] = (
                plain[rank - 1][1] if rank is not None and rank <= 5 else None
            )
            if query.filters is not None:
                filtered = passages(
                    corpus, ranking, query, filtered=True, token_counter=counter
                )
                row[f"{label}_rank_filtered"] = rank_of_expected(
                    [(u.unit.citation.source_filename, t) for u, t, _ in filtered],
                    query.expected,
                )
            if "long_unit" in query.tags:
                position, text, truncated = long_unit_rank(plain[:5], query.expected)
                row[f"{label}_long_unit_rank"] = position
                row[f"{label}_long_unit_text"] = text
                row[f"{label}_long_unit_truncated"] = truncated
        per_query.append(row)
    return {
        "queries": len(per_query),
        "dense": metrics(query_set.reference.queries, per_query, "dense"),
        "hybrid": metrics(query_set.reference.queries, per_query, "hybrid"),
        "per_query": [
            {k: v for k, v in row.items() if not k.endswith("_text")}
            for row in per_query
        ],
    }


def metrics(
    queries: Sequence[ReferenceQuery], rows: Sequence[Mapping[str, Any]], label: str
) -> dict[str, float | None]:
    by_id = {q.id: q for q in queries}
    ranks = [row[f"{label}_rank"] for row in rows]

    def subset(tag: str | None = None, *, cross: bool | None = None) -> list[Any]:
        return [
            row[f"{label}_rank"]
            for row in rows
            if (tag is None or tag in row["tags"])
            and (cross is None or by_id[row["id"]].cross_language == cross)
        ]

    def recall5(values: Sequence[int | None]) -> float | None:
        return None if not values else recall_at_k(values, 5)

    filtered = [
        row[f"{label}_rank_filtered"] for row in rows if f"{label}_rank_filtered" in row
    ]
    same_plain = [
        row[f"{label}_rank"] for row in rows if f"{label}_rank_filtered" in row
    ]
    long_rows = [row for row in rows if "long_unit" in row["tags"]]
    truncated = [
        (by_id[row["id"]].expected, row[f"{label}_long_unit_text"])
        for row in long_rows
        if row[f"{label}_long_unit_truncated"]
    ]
    result: dict[str, float | None] = {
        "recall_at_1": recall_at_k(ranks, 1),
        "recall_at_5": recall_at_k(ranks, 5),
        "mrr": mean_reciprocal_rank(ranks),
        "unit_integrity": unit_integrity(
            [(by_id[row["id"]].expected, row[f"{label}_expected_text"]) for row in rows]
        ),
        "cross_language_recall_at_5": recall5(subset(cross=True)),
        "same_language_recall_at_5": recall5(subset(cross=False)),
        "recall_at_5_with_filters": recall5(filtered),
        "recall_at_5_without_filters_same_queries": recall5(same_plain),
        "code_identifier_recall_at_5": recall5(subset("code_identifier")),
        "code_identifier_recall_at_1": (
            recall_at_k(subset("code_identifier"), 1)
            if subset("code_identifier")
            else None
        ),
        "near_negative_recall_at_5": recall5(subset("near_negative")),
        "near_negative_recall_at_1": (
            recall_at_k(subset("near_negative"), 1) if subset("near_negative") else None
        ),
    }
    if long_rows:
        result["long_unit_found_at_5"] = sum(
            1 for row in long_rows if row[f"{label}_long_unit_rank"] is not None
        ) / len(long_rows)
        result["truncated_unit_integrity"] = (
            sum(1 for expected, text in truncated if unit_is_intact(text, expected))
            / len(truncated)
            if truncated
            else None
        )
    return result


# --- orchestration ------------------------------------------------------------------


def machine() -> dict[str, Any]:
    def sysctl(name: str) -> str:
        found = subprocess.run(
            ["sysctl", "-n", name], capture_output=True, text=True, check=False
        )
        return found.stdout.strip()

    import sentence_transformers
    import torch

    return {
        "date": datetime.now(UTC).isoformat(timespec="seconds"),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu": sysctl("machdep.cpu.brand_string") or platform.processor(),
        "logical_cpus": os.cpu_count(),
        "memory_gib": round(int(sysctl("hw.memsize") or 0) / 2**30, 1),
        "python": platform.python_version(),
        "torch": torch.__version__,
        "torch_threads": torch.get_num_threads(),
        "sentence_transformers": sentence_transformers.__version__,
        "device": "cpu",
    }


def run_isolated(name: str, output: Path) -> dict[str, Any]:
    command = [sys.executable, str(Path(__file__).resolve()), "--one", name]
    command += ["--output", str(output)]
    subprocess.run(command, check=True, cwd=BACKEND)
    measured: dict[str, Any] = json.loads(output.read_text(encoding="utf-8"))
    return measured


def table(results: Iterable[Mapping[str, Any]]) -> str:
    header = (
        "| Candidat | Dim. | Poids (Mio) | RSS chargé / après encodage (Mio) | "
        "Chargement (s) | Passage (ms/chunk) | Requête méd. / p95 (ms) | "
        "S5 R@1 | S5 R@5 | S5 MRR | Inter-langues R@5 | Filtres R@5 | "
        "S6 office R@5 | Unité longue @5 | Intégrité tronquée | "
        "Identifiants R@5 (dense / hybride) | Négatifs proches R@1 |"
    )
    lines = [header, "|" + "|".join("---" for _ in range(header.count("|") - 1)) + "|"]
    for r in results:
        res = r["resources"]
        s5 = r["sets"]["s5"]["dense"]
        office = r["sets"]["s6_office"]["dense"]
        long = r["sets"]["s6_long_units"]["dense"]
        sup = r["sets"]["s9_supplement"]
        lines.append(
            f"| {r['name']} | {r['profile']['dimension']} | {res['weights_mib']} | "
            f"{res['rss_after_load_mib']} / {res['rss_after_encoding_mib']} | "
            f"{res['load_seconds']} | {res['passage_ms_per_chunk_median']} | "
            f"{res['query_ms_median']} / {res['query_ms_p95']} | "
            f"{fmt(s5['recall_at_1'])} | {fmt(s5['recall_at_5'])} | "
            f"{fmt(s5['mrr'])} | {fmt(s5['cross_language_recall_at_5'])} | "
            f"{fmt(s5['recall_at_5_with_filters'])} | {fmt(office['recall_at_5'])} | "
            f"{fmt(long.get('long_unit_found_at_5'))} | "
            f"{fmt(long.get('truncated_unit_integrity'))} | "
            f"{fmt(sup['dense']['code_identifier_recall_at_5'])} / "
            f"{fmt(sup['hybrid']['code_identifier_recall_at_5'])} | "
            f"{fmt(sup['dense']['near_negative_recall_at_1'])} |"
        )
    return "\n".join(lines)


def fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark des profils d'embeddings (S9)"
    )
    parser.add_argument("names", nargs="*", help=f"candidats : {list(CANDIDATES)}")
    parser.add_argument("--one", help=argparse.SUPPRESS)
    parser.add_argument("--output", type=Path, help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    if arguments.one:
        result = measure(arguments.one)
        arguments.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return 0
    names = arguments.names or list(CANDIDATES)
    unknown = sorted(set(names) - set(CANDIDATES))
    if unknown:
        parser.error(f"candidats inconnus : {unknown} ; connus : {list(CANDIDATES)}")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    parse_all()
    results = [run_isolated(name, REPORT_DIR / f"{name}.json") for name in names]
    report = {"machine": machine(), "results": results}
    path = REPORT_DIR / "embedding_benchmark.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    sys.stdout.write(table(results) + "\n")
    sys.stdout.write(f"\nrapport : {path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
