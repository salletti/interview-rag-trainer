"""Reconstruction small-to-big d'un passage (knowledge-base.md §12 étape 6 ;
ADR 0009 §6).

Le texte rendu ne dépasse jamais ``max_tokens``, compté par ``token_counter``
(en production, le tokenizer du profil, déjà chargé par l'encodage de la
requête) : c'est la « limite explicite » de l'ADR 0009, point 6.

- Unité complète si elle tient dans le budget, question comprise : taille des
  blocs (``blocks_token_count``, comptée à l'extraction) plus la ligne
  ``Question : …``, puis, si cette somme tient, le texte rendu lui-même
  (question puis blocs). ``truncated=False``.
- Sinon, **fenêtre** de chunks contigus, ``truncated=True``. La ligne de
  question est réservée d'abord : entière si elle tient dans la moitié du
  budget, sinon tronquée aux mots (suivie de `` …``) à cette moitié, comme
  l'en-tête d'un chunk par le ``ChunkPlanner`` ; le reste du budget est celui
  de la fenêtre (review indépendante, constat 2 : la question intégrale était
  ajoutée après le choix de la fenêtre). La fenêtre part du meilleur chunk
  trouvé et s'étend d'un voisin à la fois tant que la somme des
  ``token_count`` des chunks tient. Voisin choisi : un chunk trouvé (le mieux
  classé), sinon celui qui rapproche du meilleur chunk trouvé hors fenêtre,
  sinon le suivant (la suite de la réponse), sinon le précédent. Un chunk
  seul plus grand que son budget est retenu seul.
- La fenêtre garde la question racine en tête, puis le contenu de chaque
  chunk dans l'ordre de lecture, sans son en-tête contextuel, entier ou tronqué
  par le ``ChunkPlanner`` (il répéterait titre, chemin et question à chaque
  chunk). Le budget est compté sur les ``token_count`` des chunks, en-têtes
  compris : le texte rendu est donc en général plus court que le budget.
- **Contrôle final** : le texte de la fenêtre est compté ; s'il dépasse
  encore (chunk seul plus grand que son budget, compteur non additif), il est
  tronqué aux mots au budget, suivi de `` …``.
- ``matched_chunk_ids`` : les chunks trouvés présents dans le passage (au
  moins leur début), le meilleur d'abord.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from knowledge_base.domain.providers.token_counter import TokenCounter
from knowledge_base.domain.services.chunk_planner import (
    HEADER_SEPARATOR,
    QUESTION_LABEL,
    TRUNCATION_MARK,
    contextual_header,
    strip_contextual_header,
    truncate_at_words,
)
from knowledge_base.domain.value_objects.chunk_hit import ChunkHit
from knowledge_base.domain.value_objects.published_unit import PublishedUnit


@dataclass(frozen=True)
class AssembledPassage:
    text: str
    truncated: bool
    matched_chunk_ids: tuple[UUID, ...]


def assemble_passage(
    unit: PublishedUnit,
    hits: Sequence[ChunkHit],
    *,
    max_tokens: int,
    token_counter: TokenCounter,
) -> AssembledPassage:
    """``hits`` : les chunks trouvés de ``unit`` (au moins un)."""
    if not hits:
        raise ValueError(f"aucun chunk trouvé pour l'unité {unit.unit_id}")
    ranked = sorted(hits, key=lambda hit: hit.score, reverse=True)
    whole = _joined(_question_line(unit), [unit.text])
    if (
        whole_unit_token_count(unit, token_counter) <= max_tokens
        and token_counter.count(whole) <= max_tokens
    ):
        return AssembledPassage(
            text=whole,
            truncated=False,
            matched_chunk_ids=tuple(hit.chunk_id for hit in ranked),
        )
    return _window_passage(unit, ranked, max_tokens, token_counter)


def whole_unit_token_count(unit: PublishedUnit, token_counter: TokenCounter) -> int:
    """Taille du passage complet : blocs, plus la ligne de question."""
    question_line = _question_line(unit)
    if question_line is None:
        return unit.blocks_token_count
    return unit.blocks_token_count + token_counter.count(question_line)


def _window_passage(
    unit: PublishedUnit,
    ranked: Sequence[ChunkHit],
    max_tokens: int,
    token_counter: TokenCounter,
) -> AssembledPassage:
    question = _window_question(unit, max_tokens, token_counter)
    question_tokens = 0 if question is None else token_counter.count(question)
    first, last = _window(unit, ranked, max_tokens - question_tokens)
    kept = unit.chunks[first : last + 1]
    header = contextual_header(
        unit.citation.document_title, unit.citation.section_path, unit.question_text
    )
    bodies = [strip_contextual_header(chunk.text, header) for chunk in kept]
    text = _joined(question, bodies)
    starts = _body_starts(question, bodies)
    if token_counter.count(text) > max_tokens:
        text = truncate_at_words(text, max_tokens, token_counter)
        present = len(text.removesuffix(TRUNCATION_MARK).rstrip())
        kept = tuple(
            chunk for chunk, start in zip(kept, starts, strict=True) if start < present
        )
    kept_ids = {chunk.chunk_id for chunk in kept}
    return AssembledPassage(
        text=text,
        truncated=True,
        matched_chunk_ids=tuple(h.chunk_id for h in ranked if h.chunk_id in kept_ids),
    )


def _question_line(unit: PublishedUnit) -> str | None:
    if unit.question_text is None:
        return None
    return f"{QUESTION_LABEL}{unit.question_text}"


def _window_question(
    unit: PublishedUnit, max_tokens: int, token_counter: TokenCounter
) -> str | None:
    """Ligne de question d'une fenêtre : au plus la moitié du budget."""
    question_line = _question_line(unit)
    if question_line is None:
        return None
    limit = max_tokens // 2
    if token_counter.count(question_line) <= limit:
        return question_line
    return truncate_at_words(question_line, limit, token_counter)


def _joined(question: str | None, bodies: Sequence[str]) -> str:
    parts = bodies if question is None else [question, *bodies]
    return HEADER_SEPARATOR.join(parts)


def _body_starts(question: str | None, bodies: Sequence[str]) -> list[int]:
    """Position de chaque corps dans ``_joined(question, bodies)``."""
    position = 0 if question is None else len(question) + len(HEADER_SEPARATOR)
    starts: list[int] = []
    for body in bodies:
        starts.append(position)
        position += len(body) + len(HEADER_SEPARATOR)
    return starts


def _window(
    unit: PublishedUnit, ranked: Sequence[ChunkHit], max_tokens: int
) -> tuple[int, int]:
    """Bornes incluses de la fenêtre dans ``unit.chunks``."""
    position = {chunk.chunk_id: rank for rank, chunk in enumerate(unit.chunks)}
    unknown = [hit.chunk_id for hit in ranked if hit.chunk_id not in position]
    if unknown:
        # Les points de l'index sont dérivés des chunks de l'unité (I7) : un
        # chunk inconnu signale un index ou une base incohérents.
        raise ValueError(
            f"chunks trouvés absents de l'unité {unit.unit_id} : {unknown}"
        )
    matched = [position[hit.chunk_id] for hit in ranked]
    sizes = [chunk.token_count for chunk in unit.chunks]
    first = last = matched[0]
    used = sizes[first]
    while True:
        fitting = [
            rank
            for rank in (last + 1, first - 1)
            if 0 <= rank < len(sizes) and used + sizes[rank] <= max_tokens
        ]
        if not fitting:
            return first, last
        chosen = _next_neighbour(fitting, matched, first, last)
        used += sizes[chosen]
        first, last = min(first, chosen), max(last, chosen)


def _next_neighbour(
    fitting: Sequence[int], matched: Sequence[int], first: int, last: int
) -> int:
    """``fitting`` : voisins qui tiennent, le suivant d'abord ; ``matched`` :
    rangs des chunks trouvés, le meilleur d'abord."""
    for rank in matched:
        if rank in fitting:
            return rank
    outside = [rank for rank in matched if not first <= rank <= last]
    if outside:
        towards = last + 1 if outside[0] > last else first - 1
        if towards in fitting:
            return towards
    return fitting[0]
