"""Reconstruction small-to-big d'un passage (knowledge-base.md §12 étape 6 ;
ADR 0009 §6).

- Unité complète si ``unit.token_count`` (question comprise) tient dans le
  budget : question (``Question : …``) puis blocs, ``truncated=False``.
- Sinon, **fenêtre** de chunks contigus, ``truncated=True`` : elle part du
  meilleur chunk trouvé et s'étend d'un voisin à la fois tant que la somme des
  ``token_count`` des chunks tient dans le budget. Voisin choisi : un chunk
  trouvé (le mieux classé), sinon celui qui rapproche du meilleur chunk trouvé
  hors fenêtre, sinon le suivant (la suite de la réponse), sinon le précédent.
  Un chunk seul plus grand que le budget est rendu seul.
- La fenêtre garde la question racine en tête, puis le contenu de chaque
  chunk dans l'ordre de lecture, sans son en-tête contextuel quand celui-ci est
  reconnu (il répéterait titre, chemin et question à chaque chunk). Le budget
  est compté sur les ``token_count`` des chunks, en-têtes compris : le texte
  rendu est donc plus court que le budget compté.
- ``matched_chunk_ids`` : les chunks trouvés présents dans le passage, le
  meilleur d'abord.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from knowledge_base.domain.services.chunk_planner import (
    HEADER_SEPARATOR,
    QUESTION_LABEL,
    contextual_header,
)
from knowledge_base.domain.value_objects.chunk_hit import ChunkHit
from knowledge_base.domain.value_objects.published_unit import (
    PublishedUnit,
    UnitChunk,
)


@dataclass(frozen=True)
class AssembledPassage:
    text: str
    truncated: bool
    matched_chunk_ids: tuple[UUID, ...]


def assemble_passage(
    unit: PublishedUnit, hits: Sequence[ChunkHit], max_tokens: int
) -> AssembledPassage:
    """``hits`` : les chunks trouvés de ``unit`` (au moins un)."""
    if not hits:
        raise ValueError(f"aucun chunk trouvé pour l'unité {unit.unit_id}")
    ranked = sorted(hits, key=lambda hit: hit.score, reverse=True)
    if unit.token_count <= max_tokens:
        return AssembledPassage(
            text=_with_question(unit, unit.text),
            truncated=False,
            matched_chunk_ids=tuple(hit.chunk_id for hit in ranked),
        )
    first, last = _window(unit, ranked, max_tokens)
    kept = unit.chunks[first : last + 1]
    kept_ids = {chunk.chunk_id for chunk in kept}
    header = contextual_header(
        unit.citation.document_title, unit.citation.section_path, unit.question_text
    )
    body = HEADER_SEPARATOR.join(_content(chunk, header) for chunk in kept)
    return AssembledPassage(
        text=_with_question(unit, body),
        truncated=True,
        matched_chunk_ids=tuple(h.chunk_id for h in ranked if h.chunk_id in kept_ids),
    )


def _with_question(unit: PublishedUnit, body: str) -> str:
    if unit.question_text is None:
        return body
    return f"{QUESTION_LABEL}{unit.question_text}{HEADER_SEPARATOR}{body}"


def _content(chunk: UnitChunk, header: str) -> str:
    """Contenu du chunk sans son en-tête ; texte entier si l'en-tête n'est pas
    reconnu (en-tête tronqué par le ``ChunkPlanner``)."""
    prefix = f"{header}{HEADER_SEPARATOR}"
    if chunk.text.startswith(prefix):
        return chunk.text[len(prefix) :]
    return chunk.text


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
