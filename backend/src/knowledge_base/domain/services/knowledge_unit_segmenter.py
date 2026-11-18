"""``KnowledgeUnitSegmenter`` : ``StructuredDocument`` -> ``KnowledgeUnit``
selon le ``DocumentKind`` (ADR 0009 §1-2 ; point ouvert O13).

- ``QA_SHEET`` : une unité ``QA`` par question (question + réponse + code +
  explications), le reste en unités ``SECTION`` ; sans aucune question, le
  document est découpé comme un cours.
- ``COURSE`` / ``TECHNICAL`` : une section avec ses sous-sections tant que le
  tout tient dans le budget, sinon descente dans les sous-sections.
- ``REVISION_SHEET`` : une unité ``DEFINITION`` par section la plus haute qui
  porte du corps, sous-sections comprises.

Le front-matter ``kind: qa_sheet`` l'emporte sur le ``DocumentKind`` déclaré
et sur l'heuristique : tout titre de niveau 2 est alors une question.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Final
from uuid import UUID

from knowledge_base.domain.providers.token_counter import TokenCounter
from knowledge_base.domain.services.identifiers import unit_id
from knowledge_base.domain.value_objects.chunking_policy import ChunkingPolicy
from knowledge_base.domain.value_objects.document_kind import DocumentKind
from knowledge_base.domain.value_objects.knowledge_unit import KnowledgeUnit, UnitType
from knowledge_base.domain.value_objects.page_span import PageSpan
from knowledge_base.domain.value_objects.section_path import SectionPath
from knowledge_base.domain.value_objects.structured_document import (
    Block,
    Heading,
    StructuredDocument,
    render_blocks,
)

EXPLICIT_QA_KIND: Final = "qa_sheet"
EXPLICIT_QUESTION_LEVEL: Final = 2

# « Question : … », « Q: … », « Question 2 : … », « q2: … ».
_QUESTION_PREFIX: Final = re.compile(
    r"^\s*(?:question|q)\s*\d*\s*:\s*(?P<text>.*\S)", re.IGNORECASE
)

_QuestionRule = Callable[[Heading], bool]


@dataclass(frozen=True)
class _Draft:
    """Unité en construction, avant attribution de son rang."""

    unit_type: UnitType
    question: str | None
    path: tuple[str, ...]
    blocks: tuple[Block, ...]


@dataclass
class _Section:
    """Nœud de l'arbre des titres : ``body`` est le corps propre (avant le
    premier sous-titre), ``children`` les sous-sections dans l'ordre."""

    heading: Heading | None
    path: tuple[str, ...]
    body: list[Block] = field(default_factory=list)
    children: list[_Section] = field(default_factory=list)

    def all_blocks(self) -> list[Block]:
        """Corps et sous-sections (titres compris), dans l'ordre du document."""
        blocks = list(self.body)
        for child in self.children:
            assert child.heading is not None
            blocks.append(child.heading)
            blocks.extend(child.all_blocks())
        return blocks


class KnowledgeUnitSegmenter:
    def __init__(self, token_counter: TokenCounter, policy: ChunkingPolicy) -> None:
        self._counter = token_counter
        self._policy = policy

    def segment(
        self, document: StructuredDocument, *, kind: DocumentKind, version_id: UUID
    ) -> Sequence[KnowledgeUnit]:
        drafts = list(self._drafts(document, kind))
        return tuple(
            self._unit(draft, ordinal, version_id)
            for ordinal, draft in enumerate(drafts)
        )

    def _drafts(
        self, document: StructuredDocument, kind: DocumentKind
    ) -> Iterator[_Draft]:
        blocks = document.blocks
        explicit = document.metadata.get("kind", "").strip().lower() == EXPLICIT_QA_KIND
        if explicit or kind is DocumentKind.QA_SHEET:
            is_question = _explicit_question if explicit else _heuristic_question
            if any(isinstance(b, Heading) and is_question(b) for b in blocks):
                yield from _qa_drafts(blocks, is_question)
                return
        root = _section_tree(blocks)
        if kind is DocumentKind.REVISION_SHEET and not explicit:
            yield from _definition_drafts(root)
        else:
            yield from self._section_drafts(root)

    def _section_drafts(self, root: _Section) -> Iterator[_Draft]:
        # La racine ne fusionne jamais : son corps est une unité à part.
        yield from _draft_if_content(UnitType.SECTION, root.path, root.body)
        for child in root.children:
            yield from self._descend(child)

    def _descend(self, section: _Section) -> Iterator[_Draft]:
        whole = section.all_blocks()
        if self._fits(whole):
            yield from _draft_if_content(UnitType.SECTION, section.path, whole)
            return
        yield from _draft_if_content(UnitType.SECTION, section.path, section.body)
        for child in section.children:
            yield from self._descend(child)

    def _fits(self, blocks: Sequence[Block]) -> bool:
        return (
            self._counter.count(render_blocks(tuple(blocks))) <= self._policy.max_tokens
        )

    def _unit(self, draft: _Draft, ordinal: int, version_id: UUID) -> KnowledgeUnit:
        return KnowledgeUnit(
            unit_id=unit_id(version_id, self._policy.version, ordinal),
            ordinal=ordinal,
            unit_type=draft.unit_type,
            question_text=draft.question,
            section_path=SectionPath(draft.path),
            page_span=PageSpan.covering(b.page for b in draft.blocks),
            token_count=self._counter.count(render_blocks(draft.blocks)),
            blocks=draft.blocks,
        )


# --- questions ------------------------------------------------------------------


def _explicit_question(heading: Heading) -> bool:
    return heading.level == EXPLICIT_QUESTION_LEVEL


def _heuristic_question(heading: Heading) -> bool:
    text = heading.text.rstrip()
    return _QUESTION_PREFIX.match(text) is not None or text.endswith("?")


def _question_text(heading: Heading) -> str:
    """Le titre sans son préfixe « Question : » / « Q: » éventuel."""
    match = _QUESTION_PREFIX.match(heading.text)
    return match["text"] if match else heading.text.strip()


def _qa_drafts(blocks: Sequence[Block], is_question: _QuestionRule) -> Iterator[_Draft]:
    """Une question ouvre une unité ``QA`` qui court jusqu'à la question
    suivante ou un titre de niveau strictement inférieur ; les titres du corps
    (« Réponse », « Explication ») restent des blocs et n'entrent pas dans le
    chemin. Tout contenu hors question forme une unité ``SECTION``."""
    stack: list[Heading] = []
    current: _OpenUnit | None = None

    def close() -> Iterator[_Draft]:
        if current is not None:
            yield from current.draft()

    for block in blocks:
        if not isinstance(block, Heading):
            if current is None:
                current = _OpenUnit(UnitType.SECTION, None, _texts(stack), level=0)
            current.blocks.append(block)
            continue
        if is_question(block):
            yield from close()
            _push(stack, block)
            current = _OpenUnit(
                UnitType.QA,
                _question_text(block),
                _texts(stack[:-1]),
                level=block.level,
            )
        elif current is not None and current.keeps(block):
            current.blocks.append(block)
        else:
            yield from close()
            current = None
            _push(stack, block)
    yield from close()


@dataclass
class _OpenUnit:
    unit_type: UnitType
    question: str | None
    path: tuple[str, ...]
    # Niveau de la question ; 0 pour une section, close par tout titre.
    level: int
    blocks: list[Block] = field(default_factory=list)

    def keeps(self, heading: Heading) -> bool:
        return self.unit_type is UnitType.QA and heading.level >= self.level

    def draft(self) -> Iterator[_Draft]:
        yield from _draft_if_content(
            self.unit_type, self.path, self.blocks, question=self.question
        )


# --- sections -------------------------------------------------------------------


def _section_tree(blocks: Sequence[Block]) -> _Section:
    root = _Section(heading=None, path=())
    open_sections = [root]
    for block in blocks:
        if isinstance(block, Heading):
            while (
                len(open_sections) > 1
                and open_sections[-1].heading is not None
                and open_sections[-1].heading.level >= block.level
            ):
                open_sections.pop()
            parent = open_sections[-1]
            section = _Section(heading=block, path=(*parent.path, block.text))
            parent.children.append(section)
            open_sections.append(section)
        else:
            open_sections[-1].body.append(block)
    return root


def _definition_drafts(root: _Section) -> Iterator[_Draft]:
    yield from _draft_if_content(UnitType.DEFINITION, root.path, root.body)
    for child in root.children:
        yield from _highest_definitions(child)


def _highest_definitions(section: _Section) -> Iterator[_Draft]:
    if _has_content(section.body):
        yield from _draft_if_content(
            UnitType.DEFINITION, section.path, section.all_blocks()
        )
        return
    for child in section.children:
        yield from _highest_definitions(child)


# --- outils ---------------------------------------------------------------------


def _push(stack: list[Heading], heading: Heading) -> None:
    while stack and stack[-1].level >= heading.level:
        stack.pop()
    stack.append(heading)


def _texts(headings: Sequence[Heading]) -> tuple[str, ...]:
    return tuple(h.text for h in headings)


def _has_content(blocks: Sequence[Block]) -> bool:
    return any(not isinstance(b, Heading) for b in blocks)


def _draft_if_content(
    unit_type: UnitType,
    path: tuple[str, ...],
    blocks: Sequence[Block],
    *,
    question: str | None = None,
) -> Iterator[_Draft]:
    """Une unité sans autre bloc que des titres n'est pas produite : elle
    n'aurait rien à apprendre."""
    if _has_content(blocks):
        yield _Draft(unit_type, question, path, tuple(blocks))
