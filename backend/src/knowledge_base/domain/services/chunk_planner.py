"""``ChunkPlanner`` : ``KnowledgeUnit`` -> ``DocumentChunk`` dans le budget en
tokens de la politique (ADR 0009 §3-5 ; knowledge-base.md §15.2 ; I13).

- ``contextual_text`` = en-tête (titre du document, chemin de sections,
  ``Question : …``) + ligne vide + blocs rendus ; son ``token_count`` complet ne
  dépasse jamais ``policy.max_tokens``.
- Remplissage séquentiel par blocs entiers ; un bloc qui ne tient pas seul est
  découpé, chaque fragment est un chunk et tout fragment sauf le premier
  commence par ``CONTINUATION_MARKER``.
- Frontières de découpe d'un bloc trop grand :

  | bloc | frontières, dans l'ordre |
  |---|---|
  | code | ``policy.fallbacks`` (lignes vides, définitions ``def``/``class``, lignes) |
  | tableau, liste | lignes (rangées, éléments) |
  | paragraphe | phrases |
  | titre | — |

  puis, pour tous, les mots et enfin les caractères : ces deux derniers
  recours garantissent le budget quel que soit le contenu.
- En-tête plus long que la moitié du budget : tronqué aux mots (suivi de
  ``…``), pour laisser au moins la moitié du budget au contenu.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Final

from knowledge_base.domain.providers.token_counter import TokenCounter
from knowledge_base.domain.services.identifiers import chunk_id
from knowledge_base.domain.value_objects.chunking_policy import (
    ChunkingPolicy,
    CodeFallback,
)
from knowledge_base.domain.value_objects.citation import Citation
from knowledge_base.domain.value_objects.document_chunk import DocumentChunk
from knowledge_base.domain.value_objects.knowledge_unit import KnowledgeUnit
from knowledge_base.domain.value_objects.page_span import PageSpan
from knowledge_base.domain.value_objects.structured_document import (
    Block,
    CodeBlock,
    Heading,
    Paragraph,
    render_block,
    render_blocks,
)

CONTINUATION_MARKER: Final = "(suite)"
TRUNCATION_MARK: Final = "…"
QUESTION_LABEL: Final = "Question : "

Fits = Callable[[str], bool]


@dataclass(frozen=True)
class _Boundary:
    """Découpe un texte en morceaux recollés par ``separator``."""

    split: Callable[[str], list[str]]
    separator: str


def _non_empty(pieces: list[str]) -> list[str]:
    return [piece for piece in pieces if piece.strip()]


_DEFINITION: Final = re.compile(r"(?:async\s+def|def|class)\b")


def _split_definitions(text: str) -> list[str]:
    """Groupes de lignes commençant chacun par une définition en colonne 0,
    décorateurs qui la précèdent compris."""
    groups: list[list[str]] = [[]]
    for line in text.split("\n"):
        if _DEFINITION.match(line):
            decorators: list[str] = []
            current = groups[-1]
            while current and current[-1].startswith("@"):
                decorators.insert(0, current.pop())
            groups.append(decorators)
        groups[-1].append(line)
    return _non_empty(["\n".join(group) for group in groups])


BLANK_LINES: Final = _Boundary(
    lambda text: _non_empty(re.split(r"\n(?:[ \t]*\n)+", text)), "\n\n"
)
DEFINITIONS: Final = _Boundary(_split_definitions, "\n")
LINES: Final = _Boundary(lambda text: _non_empty(text.split("\n")), "\n")
SENTENCES: Final = _Boundary(
    lambda text: _non_empty(re.split(r"(?<=[.!?…])\s+", text)), " "
)
WORDS: Final = _Boundary(str.split, " ")

_CODE_BOUNDARIES: Final = {
    CodeFallback.BLANK_LINES: BLANK_LINES,
    CodeFallback.DEFINITIONS: DEFINITIONS,
    CodeFallback.LINES: LINES,
}


class ChunkBudgetError(ValueError):
    """Le budget ne laisse pas même un caractère de contenu après l'en-tête et
    le marqueur : la politique est inutilisable pour ce document."""


class ChunkPlanner:
    def __init__(self, token_counter: TokenCounter, policy: ChunkingPolicy) -> None:
        if policy.overlap_tokens:
            raise ValueError(
                f"overlap_tokens={policy.overlap_tokens} non pris en charge : "
                "l'overlap est mesuré en S5/S9 (ADR 0009 §5), 0 en S3"
            )
        self._counter = token_counter
        self._policy = policy

    def plan(
        self, unit: KnowledgeUnit, citation_base: Citation
    ) -> Sequence[DocumentChunk]:
        header = self._header(unit, citation_base.document_title)
        bodies: list[tuple[str, tuple[Block, ...]]] = []
        current: list[Block] = []
        for block in unit.blocks:
            if self._fits(header, render_blocks((*current, block))):
                current.append(block)
                continue
            if current:
                bodies.append((render_blocks(tuple(current)), tuple(current)))
                current = []
            if self._fits(header, render_block(block)):
                current = [block]
                continue
            for index, fragment in enumerate(self._fragments(header, block)):
                bodies.append((_continued(fragment, index), (block,)))
        if current:
            bodies.append((render_blocks(tuple(current)), tuple(current)))
        return tuple(
            self._chunk(unit, citation_base, ordinal, f"{header}\n\n{body}", blocks)
            for ordinal, (body, blocks) in enumerate(bodies)
        )

    # --- en-tête --------------------------------------------------------------

    def _header(self, unit: KnowledgeUnit, title: str) -> str:
        lines = [title]
        if unit.section_path.parts:
            lines.append(str(unit.section_path))
        if unit.question_text is not None:
            lines.append(f"{QUESTION_LABEL}{unit.question_text}")
        header = "\n".join(lines)
        limit = self._policy.max_tokens // 2
        if self._counter.count(header) <= limit:
            return header
        return self._truncate(header, limit)

    def _truncate(self, header: str, limit: int) -> str:
        def truncated(prefix: str) -> str:
            return f"{prefix.rstrip()} {TRUNCATION_MARK}"

        def fits(prefix: str) -> bool:
            return self._counter.count(truncated(prefix)) <= limit

        word_ends = [match.end() for match in re.finditer(r"\S+", header)]
        kept = _longest_prefix(header, word_ends, fits)
        if not kept:
            kept = _longest_prefix(header, list(range(1, len(header) + 1)), fits)
        return truncated(kept) if kept else TRUNCATION_MARK

    # --- découpe d'un bloc trop grand ---------------------------------------------

    def _fragments(self, header: str, block: Block) -> list[str]:
        """Corps des fragments d'un bloc qui ne tient pas seul, sans marqueur.
        Le marqueur est réservé pour chacun, premier compris."""
        if isinstance(block, CodeBlock):
            code = block
            boundaries = [_CODE_BOUNDARIES[f] for f in self._policy.fallbacks]
            source = code.text

            def wrap(text: str) -> str:
                return render_block(replace(code, text=text))

        else:
            boundaries = _boundaries_of(block)
            source = render_block(block)

            def wrap(text: str) -> str:
                return text

        def fits(text: str) -> bool:
            return self._fits(header, f"{CONTINUATION_MARKER}\n{wrap(text)}")

        return [wrap(piece) for piece in _split(source, [*boundaries, WORDS], fits)]

    def _fits(self, header: str, body: str) -> bool:
        return self._counter.count(f"{header}\n\n{body}") <= self._policy.max_tokens

    # --- chunk ------------------------------------------------------------------

    def _chunk(
        self,
        unit: KnowledgeUnit,
        citation_base: Citation,
        ordinal: int,
        text: str,
        blocks: tuple[Block, ...],
    ) -> DocumentChunk:
        pages = PageSpan.covering(b.page for b in blocks)
        return DocumentChunk(
            chunk_id=chunk_id(
                citation_base.version_id, self._policy.version, unit.ordinal, ordinal
            ),
            unit_id=unit.unit_id,
            ordinal_in_unit=ordinal,
            contextual_text=text,
            chunk_hash=hashlib.sha256(
                (text + self._policy.version).encode()
            ).hexdigest(),
            token_count=self._counter.count(text),
            citation=replace(
                citation_base,
                section_path=unit.section_path,
                page_start=pages.start,
                page_end=pages.end,
            ),
        )


def _continued(fragment: str, index: int) -> str:
    return fragment if index == 0 else f"{CONTINUATION_MARKER}\n{fragment}"


def _boundaries_of(block: Block) -> list[_Boundary]:
    if isinstance(block, Paragraph):
        return [SENTENCES]
    if isinstance(block, Heading):
        return []
    return [LINES]  # Table : rangées ; ListBlock : éléments


def _split(text: str, boundaries: list[_Boundary], fits: Fits) -> list[str]:
    """Morceaux de ``text`` qui tiennent chacun, regroupés au plus large :
    les morceaux voisins sont recollés par le séparateur de leur frontière tant
    que le tout tient ; un morceau trop grand est découpé à la frontière
    suivante, et en dernier recours aux caractères."""
    if fits(text):
        return [text]
    if not boundaries:
        return _split_characters(text, fits)
    boundary, finer = boundaries[0], boundaries[1:]
    pieces = boundary.split(text)
    if len(pieces) <= 1:
        return _split(text, finer, fits)
    fragments: list[str] = []
    for piece in pieces:
        # Les parties d'un même morceau sont déjà regroupées au plus large :
        # seule la première peut rejoindre le fragment précédent.
        first, *rest = _split(piece, finer, fits)
        joined = f"{fragments[-1]}{boundary.separator}{first}" if fragments else ""
        if fragments and fits(joined):
            fragments[-1] = joined
        else:
            fragments.append(first)
        fragments.extend(rest)
    return fragments


def _split_characters(text: str, fits: Fits) -> list[str]:
    fragments: list[str] = []
    rest = text
    while rest:
        kept = _longest_prefix(rest, list(range(1, len(rest) + 1)), fits)
        if not kept:
            raise ChunkBudgetError(
                "budget de chunk trop petit : aucun caractère ne tient après "
                "l'en-tête et le marqueur de continuation"
            )
        fragments.append(kept)
        rest = rest[len(kept) :]
    return fragments


def _longest_prefix(text: str, cut_points: list[int], fits: Fits) -> str:
    """Plus long ``text[:cut]`` qui tient, par dichotomie sur ``cut_points``
    croissants (le compte de tokens croît avec le préfixe) ; vide si aucun."""
    low, high = 0, len(cut_points)
    while low < high:
        middle = (low + high + 1) // 2
        if fits(text[: cut_points[middle - 1]]):
            low = middle
        else:
            high = middle - 1
    return text[: cut_points[low - 1]] if low else ""
