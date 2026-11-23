"""Query ``SearchKnowledge`` (knowledge-base.md §12 ; ADR 0010 §5-6 ; O12).

1. Index actif (SQL), puis le modèle de son profil (S9 : ``embedders``) ; un
   profil que le processus ne sait pas charger est refusé
   (``SearchProfileMismatchError``) avant tout encodage, jamais remplacé par
   un autre modèle.
2. Requête encodée une fois (l'adapter ajoute ``query: `` et sort le calcul
   de la boucle asyncio).
3. Recherche groupée par unité, ``unit_limit = limit x f`` à la première passe.
4. Contrôle SQL final : seules les unités publiées de documents actifs restent.
5. Complément si des unités ont été écartées et qu'il en manque : jusqu'à
   ``complement_passes`` passes de ``manquantes x f`` unités, excluant les
   unités écartées ET déjà retenues. Arrêt anticipé si une passe rend moins
   d'unités que demandé (plus rien à trouver).

   Chaque lecture SQL porte sur les unités déjà retenues ET les nouvelles : les
   unités rendues sont donc toutes validées par la dernière lecture, un seul
   instantané où, sous I4, un document n'a qu'une version publiée. Valider
   chaque passe sur ses seuls nouveaux candidats laissait une publication
   commitée entre deux passes rendre une unité de N et une de N+1 (review
   indépendante, constat 1). Une unité retenue que la relecture écarte devient
   un rejet comme un autre : la passe suivante la remplace s'il en reste, sinon
   ``complete`` est faux. Le nombre de passes reste borné, et celui des
   lectures SQL inchangé (une par passe).
6. Passages reconstruits (small-to-big, ``passage_assembler``), classés par
   meilleur score de leurs chunks, au plus ``limit``. Le tokenizer du profil
   de l'index actif compte la question dans le budget : celui du modèle que
   l'encodage de l'étape 2 a déjà chargé.

``complete`` est faux si et seulement si des unités ont été écartées et qu'il
reste moins de ``limit`` passages : un corpus simplement trop petit pour la
requête, sans aucun rejet, donne un résultat complet (précision S5).

Aucune session SQL n'est tenue pendant l'encodage : chaque lecture ouvre et
ferme la sienne (adapters).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from knowledge_base.domain.entities.knowledge_corpus import SearchIndex
from knowledge_base.domain.providers.active_index_reader import ActiveIndexReader
from knowledge_base.domain.providers.chunk_searcher import ChunkSearcher
from knowledge_base.domain.providers.embedder_provider import (
    EmbedderProvider,
    TokenCountingEmbedder,
    UnknownEmbeddingProfileError,
)
from knowledge_base.domain.providers.published_unit_reader import (
    PublishedUnitReader,
)
from knowledge_base.domain.providers.token_counter import TokenCounter
from knowledge_base.domain.services.passage_assembler import assemble_passage
from knowledge_base.domain.value_objects.chunk_hit import ChunkHit
from knowledge_base.domain.value_objects.published_unit import PublishedUnit
from knowledge_base.use_cases.search_knowledge.errors import (
    NoActiveSearchIndexError,
    SearchProfileMismatchError,
)
from knowledge_base.use_cases.search_knowledge.search_knowledge_command import (
    SearchKnowledge,
)
from knowledge_base.use_cases.search_knowledge.search_knowledge_result import (
    FoundPassage,
    SearchKnowledgeResult,
)


@dataclass(frozen=True)
class _Retained:
    unit: PublishedUnit
    hits: tuple[ChunkHit, ...]

    @property
    def score(self) -> float:
        return max(hit.score for hit in self.hits)


class SearchKnowledgeHandler:
    def __init__(
        self,
        *,
        chunk_searcher: ChunkSearcher,
        published_units: PublishedUnitReader,
        active_indexes: ActiveIndexReader,
        embedders: EmbedderProvider,
        max_passage_tokens: int,
        oversampling_factor: int = 2,
        complement_passes: int = 1,
    ) -> None:
        """``embedders`` : le modèle du profil de l'index actif et son
        tokenizer."""
        if max_passage_tokens < 1:
            raise ValueError(f"max_passage_tokens < 1 : {max_passage_tokens}")
        if oversampling_factor < 1:
            raise ValueError(f"oversampling_factor < 1 : {oversampling_factor}")
        if complement_passes < 0:
            raise ValueError(f"complement_passes < 0 : {complement_passes}")
        self._embedders = embedders
        self._searcher = chunk_searcher
        self._published = published_units
        self._indexes = active_indexes
        self._max_passage_tokens = max_passage_tokens
        self._factor = oversampling_factor
        self._complement_passes = complement_passes

    async def handle(self, command: SearchKnowledge) -> SearchKnowledgeResult:
        index = await self._indexes.get_active_index()
        if index is None:
            raise NoActiveSearchIndexError
        model = self._model_for(index)
        vector = await model.embed_query(command.text)
        filters = command.filters

        retained: dict[UUID, _Retained] = {}
        rejected: set[UUID] = set()
        for _ in range(1 + self._complement_passes):
            unit_limit = (command.limit - len(retained)) * self._factor
            hits = await self._searcher.search(
                index.index_id,
                vector,
                unit_limit=unit_limit,
                filters=filters,
                excluded_unit_ids=frozenset(rejected | retained.keys()),
            )
            found = _by_unit(hits)
            candidates = {i: r.hits for i, r in retained.items()} | found
            published = await self._published.read_published(list(candidates))
            retained = {}
            for unit_id, unit_hits in candidates.items():
                unit = published.get(unit_id)
                if unit is None:
                    rejected.add(unit_id)
                else:
                    retained[unit_id] = _Retained(unit, unit_hits)
            if len(retained) >= command.limit or len(found) < unit_limit:
                break

        best = sorted(retained.values(), key=lambda r: r.score, reverse=True)
        passages = tuple(
            self._passage(r, model.token_counter) for r in best[: command.limit]
        )
        return SearchKnowledgeResult(
            passages=passages,
            complete=not rejected or len(passages) >= command.limit,
            index_id=index.index_id,
        )

    def _model_for(self, index: SearchIndex) -> TokenCountingEmbedder:
        try:
            return self._embedders.for_profile(index.profile)
        except UnknownEmbeddingProfileError:
            raise SearchProfileMismatchError(index.index_id, index.profile) from None

    def _passage(
        self, retained: _Retained, token_counter: TokenCounter
    ) -> FoundPassage:
        unit = retained.unit
        assembled = assemble_passage(
            unit,
            retained.hits,
            max_tokens=self._max_passage_tokens,
            token_counter=token_counter,
        )
        return FoundPassage(
            unit_id=unit.unit_id,
            unit_type=unit.unit_type,
            question=unit.question_text,
            text=assembled.text,
            truncated=assembled.truncated,
            matched_chunk_ids=assembled.matched_chunk_ids,
            score=retained.score,
            citation=unit.citation,
        )


def _by_unit(hits: Sequence[ChunkHit]) -> dict[UUID, tuple[ChunkHit, ...]]:
    """Chunks regroupés par unité, dans l'ordre de première apparition."""
    grouped: dict[UUID, list[ChunkHit]] = {}
    for hit in hits:
        grouped.setdefault(hit.unit_id, []).append(hit)
    return {unit_id: tuple(unit_hits) for unit_id, unit_hits in grouped.items()}
