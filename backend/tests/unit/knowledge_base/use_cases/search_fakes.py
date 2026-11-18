"""Doubles et constructeurs de S5 (``SearchKnowledge``), sans I/O.

Seules les frontières sont remplacées : l'``Embedder`` (qui enregistre le texte
reçu), le moteur vectoriel (``FakeChunkSearcher``), la lecture SQL des unités
publiées (``FakePublishedUnits``) et l'index actif. La logique testée (contrôle
SQL, complément, regroupement, small-to-big, plafond) est celle du VRAI
``SearchKnowledgeHandler``.

Ports et types NOUVEAUX figés par ces doubles (S5 ; knowledge-base.md §12, §14) :

- ``knowledge_base.domain.value_objects.search_filters.SearchFilters(language:
  str | None = None, topics: tuple[str, ...] = (), kinds: tuple[str, ...] = (),
  document_ids: tuple[UUID, ...] = ())`` (``kinds`` : valeurs de
  ``DocumentKind``, ex. ``"QA_SHEET"``).
- ``knowledge_base.domain.value_objects.chunk_hit.ChunkHit(chunk_id: UUID,
  unit_id: UUID, score: float)`` : un chunk trouvé et son score de similarité.
- ``knowledge_base.domain.providers.chunk_searcher.ChunkSearcher`` : ``await
  search(index_id: UUID, vector: Sequence[float], *, unit_limit: int, filters:
  SearchFilters, excluded_unit_ids: Collection[UUID]) -> Sequence[ChunkHit]``.
  Contrat : au plus ``unit_limit`` unités DISTINCTES (recherche groupée par
  ``unit_id``, §12 étape 3), les meilleures d'abord, avec les chunks trouvés de
  chacune ; seuls les points ``visible=true`` et conformes aux filtres ; aucune
  unité de ``excluded_unit_ids``. Implémenté par ``QdrantVectorIndex`` (le
  filtre ``visible`` est TOUJOURS appliqué par l'adapter, jamais par l'appelant).
- ``knowledge_base.domain.providers.active_index_reader.ActiveIndexReader`` :
  ``await get_active_index() -> SearchIndex | None`` (lecture SQL seule, sans
  verrou).
- ``knowledge_base.domain.providers.published_unit_reader.PublishedUnitReader`` :
  ``await read_published(unit_ids: Sequence[UUID]) -> Mapping[UUID,
  PublishedUnit]``. C'EST LE CONTRÔLE SQL FINAL (§12 étape 4) : seules les unités
  dont la version est ``PUBLISHED`` ET dont le document est ``ACTIVE`` figurent
  dans le résultat ; les autres sont absentes (jamais ``None``).
- ``knowledge_base.domain.value_objects.published_unit`` : ``PublishedUnit(
  unit_id, unit_type: UnitType, question_text: str | None, text: str,
  token_count: int, chunks: tuple[UnitChunk, ...], citation: Citation)`` ;
  ``text`` = blocs rendus de l'unité SANS la question (colonne
  ``knowledge_units.text``) ; ``chunks`` dans l'ordre de rang ; ``citation`` =
  citation du domaine (``section_path`` et pages de l'UNITÉ). ``UnitChunk(
  chunk_id, ordinal_in_unit: int, text: str, token_count: int)`` où ``text`` est
  le ``contextual_text`` du chunk (en-tête + contenu).

Cas d'usage (``knowledge_base.use_cases.search_knowledge``) :

- ``search_knowledge_command.SearchKnowledge(text, limit=5, language=None,
  topics=(), kinds=(), document_ids=())`` ; ``text`` vide ou blanc et
  ``limit < 1`` lèvent ``ValueError`` à la construction.
- ``search_knowledge_handler.SearchKnowledgeHandler(*, embedder,
  chunk_searcher, published_units, active_indexes, max_passage_tokens,
  oversampling_factor=2, complement_passes=1)`` ; ``await handle(command) ->
  SearchKnowledgeResult``.
- ``search_knowledge_result.SearchKnowledgeResult(passages: tuple[FoundPassage,
  ...], complete: bool, index_id: UUID)`` et ``FoundPassage(unit_id, unit_type,
  question, text, truncated, matched_chunk_ids, score, citation)`` : les champs
  du contrat public (§14) mais avec les types du domaine (``UnitType``,
  ``Citation`` du domaine) ; le cas d'usage n'importe JAMAIS ``contracts/``.
- ``errors.NoActiveSearchIndexError`` : aucun index actif.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from knowledge_base.domain.entities.knowledge_corpus import IndexStatus, SearchIndex
from knowledge_base.domain.value_objects.chunk_hit import ChunkHit
from knowledge_base.domain.value_objects.citation import Citation
from knowledge_base.domain.value_objects.embedding_profile import EmbeddingProfile
from knowledge_base.domain.value_objects.knowledge_unit import UnitType
from knowledge_base.domain.value_objects.published_unit import (
    PublishedUnit,
    UnitChunk,
)
from knowledge_base.domain.value_objects.search_filters import SearchFilters
from knowledge_base.domain.value_objects.section_path import SectionPath
from knowledge_base.use_cases.search_knowledge.search_knowledge_handler import (
    SearchKnowledgeHandler,
)
from tests.unit.knowledge_base.domain.services.support import policy
from tests.unit.knowledge_base.use_cases.indexing_fakes import PROFILE

INDEX_ID = UUID(int=9_001)
DOCUMENT_ID = UUID(int=100)
VERSION_ID = UUID(int=200)
QUERY_VECTOR = [0.25, 0.5, 0.75]
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def uid(n: int) -> UUID:
    return UUID(int=10_000 + n)


def chunk_uid(unit: int, rank: int) -> UUID:
    return UUID(int=20_000 + unit * 100 + rank)


def citation_of(
    unit: int,
    *,
    title: str = "Fiche FastAPI",
    version_number: int = 3,
    filename: str = "fastapi.md",
) -> Citation:
    return Citation(
        document_id=UUID(int=100 + unit),
        document_title=title,
        version_id=UUID(int=200 + unit),
        version_number=version_number,
        section_path=SectionPath((title, f"Section {unit}")),
        page_start=unit,
        page_end=unit + 1,
        source_filename=filename,
    )


def whole_unit(
    n: int,
    *,
    question: str | None = None,
    body: str | None = None,
    token_count: int = 50,
    chunk_count: int = 1,
    unit_type: UnitType | None = None,
    section: bool = False,
) -> PublishedUnit:
    """Unité de ``chunk_count`` chunks de ``token_count // chunk_count`` tokens.

    Chaque chunk reprend l'en-tête contextuel réel du ``ChunkPlanner`` (titre,
    chemin, ``Question : …``) puis un marqueur unique ``<BODY-n-rang>``."""
    question = f"Question numéro {n} ?" if question is None else question
    if section:
        unit_type, question_text = UnitType.SECTION, None
    else:
        unit_type, question_text = unit_type or UnitType.QA, question
    header = f"Fiche FastAPI > Section {n}\n"
    if question_text is not None:
        header += f"Question : {question_text}\n"
    body = body if body is not None else f"Réponse de l'unité {n}."
    return PublishedUnit(
        unit_id=uid(n),
        unit_type=unit_type,
        question_text=question_text,
        text=body,
        token_count=token_count,
        chunks=tuple(
            UnitChunk(
                chunk_id=chunk_uid(n, rank),
                ordinal_in_unit=rank,
                text=f"{header}\n<BODY-{n}-{rank}>",
                token_count=token_count // chunk_count,
            )
            for rank in range(chunk_count)
        ),
        citation=citation_of(n),
    )


def hit(unit: int, score: float, rank: int = 0) -> ChunkHit:
    return ChunkHit(chunk_id=chunk_uid(unit, rank), unit_id=uid(unit), score=score)


def active_index() -> SearchIndex:
    return SearchIndex(
        index_id=INDEX_ID,
        profile=PROFILE,
        chunking_policy=policy(),
        status=IndexStatus.ACTIVE,
        created_at=NOW,
        activated_at=NOW,
        retired_at=None,
    )


class RecordingEmbedder:
    """``Embedder`` qui enregistre les textes de requête reçus."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    @property
    def profile(self) -> EmbeddingProfile:
        return PROFILE

    async def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        raise AssertionError("la recherche n'encode jamais de passage")

    async def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return list(QUERY_VECTOR)


@dataclass(frozen=True)
class SearchCall:
    index_id: UUID
    vector: tuple[float, ...]
    unit_limit: int
    filters: SearchFilters
    excluded_unit_ids: frozenset[UUID]


@dataclass
class FakeChunkSearcher:
    """Moteur vectoriel en mémoire : ``pool`` est l'ordre de classement (les
    meilleurs d'abord). Respecte le contrat du port : unités distinctes dans
    l'ordre d'apparition, au plus ``unit_limit``, hors ``excluded_unit_ids`` ;
    renvoie tous les chunks trouvés des unités retenues."""

    pool: Sequence[ChunkHit]
    calls: list[SearchCall] = field(default_factory=list)

    async def search(
        self,
        index_id: UUID,
        vector: Sequence[float],
        *,
        unit_limit: int,
        filters: SearchFilters,
        excluded_unit_ids: Collection[UUID],
    ) -> Sequence[ChunkHit]:
        excluded = frozenset(excluded_unit_ids)
        self.calls.append(
            SearchCall(index_id, tuple(vector), unit_limit, filters, excluded)
        )
        kept: list[UUID] = []
        for found in self.pool:
            if found.unit_id in excluded or found.unit_id in kept:
                continue
            if len(kept) == unit_limit:
                break
            kept.append(found.unit_id)
        return [found for found in self.pool if found.unit_id in kept]


@dataclass
class FakePublishedUnits:
    """Contrôle SQL : seules les unités de ``units`` sont publiées ; une unité
    absente du dictionnaire est une unité dont la version n'est plus publiée."""

    units: Mapping[UUID, PublishedUnit]
    calls: list[tuple[UUID, ...]] = field(default_factory=list)

    async def read_published(
        self, unit_ids: Sequence[UUID]
    ) -> Mapping[UUID, PublishedUnit]:
        self.calls.append(tuple(unit_ids))
        return {i: self.units[i] for i in unit_ids if i in self.units}


class FakeActiveIndexes:
    def __init__(self, index: SearchIndex | None) -> None:
        self._index = index

    async def get_active_index(self) -> SearchIndex | None:
        return self._index


@dataclass
class SearchRig:
    handler: SearchKnowledgeHandler
    embedder: RecordingEmbedder
    searcher: FakeChunkSearcher
    units: FakePublishedUnits


def make_rig(
    *,
    ranking: Sequence[ChunkHit],
    published: Sequence[PublishedUnit],
    max_passage_tokens: int = 1_000,
    oversampling_factor: int = 2,
    complement_passes: int = 1,
    index: SearchIndex | None = None,
    with_index: bool = True,
) -> SearchRig:
    embedder = RecordingEmbedder()
    searcher = FakeChunkSearcher(ranking)
    units = FakePublishedUnits({u.unit_id: u for u in published})
    handler = SearchKnowledgeHandler(
        embedder=embedder,
        chunk_searcher=searcher,
        published_units=units,
        active_indexes=FakeActiveIndexes(
            (index or active_index()) if with_index else None
        ),
        max_passage_tokens=max_passage_tokens,
        oversampling_factor=oversampling_factor,
        complement_passes=complement_passes,
    )
    return SearchRig(handler, embedder, searcher, units)
