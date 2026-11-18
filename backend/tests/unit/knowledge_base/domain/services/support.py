"""Fabriques et compteur de tokens de test des services de segmentation (S3).

Le compteur de test compte les mots (``str.split``) : déterministe, sans
tokenizer, et les budgets des tests se calculent à la main avec ``words(n)``.
Le contexte d'un chunk (``header``) suit le format figé par
``test_chunk_planner`` : une ligne par élément, séparé du corps par une ligne
vide.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from knowledge_base.domain.services.chunk_planner import ChunkPlanner
from knowledge_base.domain.services.knowledge_unit_segmenter import (
    KnowledgeUnitSegmenter,
)
from knowledge_base.domain.value_objects.chunking_policy import ChunkingPolicy
from knowledge_base.domain.value_objects.citation import Citation
from knowledge_base.domain.value_objects.document_chunk import DocumentChunk
from knowledge_base.domain.value_objects.document_kind import DocumentKind
from knowledge_base.domain.value_objects.knowledge_unit import KnowledgeUnit, UnitType
from knowledge_base.domain.value_objects.page_span import PageSpan
from knowledge_base.domain.value_objects.section_path import SectionPath
from knowledge_base.domain.value_objects.structured_document import (
    Block,
    CodeBlock,
    Heading,
    Paragraph,
    StructuredDocument,
    render_blocks,
)

DOCUMENT_ID = UUID(int=11)
VERSION_ID = UUID(int=22)
TITLE = "Python avancé"
FILENAME = "python.md"
POLICY_VERSION = "chunking-test-1"
# Nom du compteur de test (``WordCounter``) dans l'identité de la politique.
TOKENIZER = "test-words-v1"
# ``ChunkingPolicy.identity`` attendue : ce qui est persisté comme
# ``chunking_policy_version`` (version et tokenizer).
POLICY_IDENTITY = "chunking-test-1+test-words-v1"


class WordCounter:
    """``TokenCounter`` de test : un token par mot."""

    def count(self, text: str) -> int:
        return len(text.split())


COUNTER = WordCounter()


def words(n: int, prefix: str = "w") -> str:
    """``n`` mots distincts : ``w0 w1 ... w{n-1}``."""
    return " ".join(f"{prefix}{i}" for i in range(n))


def policy(
    max_tokens: int = 1000,
    version: str = POLICY_VERSION,
    tokenizer: str = TOKENIZER,
) -> ChunkingPolicy:
    return ChunkingPolicy(version=version, tokenizer=tokenizer, max_tokens=max_tokens)


def h(level: int, text: str, page: int | None = None) -> Heading:
    return Heading(level=level, text=text, page=page)


def p(text: str, page: int | None = None) -> Paragraph:
    return Paragraph(text=text, page=page)


def code(
    text: str, language: str | None = "python", page: int | None = None
) -> CodeBlock:
    return CodeBlock(text=text, language=language, page=page)


def doc(*blocks: Block, **metadata: str) -> StructuredDocument:
    return StructuredDocument(blocks=tuple(blocks), metadata=dict(metadata))


def segment(
    document: StructuredDocument,
    kind: DocumentKind,
    *,
    max_tokens: int = 1000,
    version_id: UUID = VERSION_ID,
    policy_version: str = POLICY_VERSION,
    tokenizer: str = TOKENIZER,
) -> tuple[KnowledgeUnit, ...]:
    segmenter = KnowledgeUnitSegmenter(
        COUNTER, policy(max_tokens, version=policy_version, tokenizer=tokenizer)
    )
    return tuple(segmenter.segment(document, kind=kind, version_id=version_id))


def base_citation(version_id: UUID = VERSION_ID) -> Citation:
    """Citation de l'import : le planificateur y met chemin et pages du chunk."""
    return Citation(
        document_id=DOCUMENT_ID,
        document_title=TITLE,
        version_id=version_id,
        version_number=3,
        section_path=SectionPath(()),
        page_start=None,
        page_end=None,
        source_filename=FILENAME,
    )


def plan(
    unit: KnowledgeUnit,
    *,
    max_tokens: int = 1000,
    version_id: UUID = VERSION_ID,
    policy_version: str = POLICY_VERSION,
    tokenizer: str = TOKENIZER,
) -> tuple[DocumentChunk, ...]:
    planner = ChunkPlanner(
        COUNTER, policy(max_tokens, version=policy_version, tokenizer=tokenizer)
    )
    return tuple(planner.plan(unit, base_citation(version_id)))


def pipeline(
    document: StructuredDocument,
    kind: DocumentKind,
    *,
    max_tokens: int = 1000,
    version_id: UUID = VERSION_ID,
    policy_version: str = POLICY_VERSION,
    tokenizer: str = TOKENIZER,
) -> tuple[tuple[KnowledgeUnit, ...], tuple[DocumentChunk, ...]]:
    units = segment(
        document,
        kind,
        max_tokens=max_tokens,
        version_id=version_id,
        policy_version=policy_version,
        tokenizer=tokenizer,
    )
    chunks = tuple(
        chunk
        for unit in units
        for chunk in plan(
            unit,
            max_tokens=max_tokens,
            version_id=version_id,
            policy_version=policy_version,
            tokenizer=tokenizer,
        )
    )
    return units, chunks


def header(
    title: str = TITLE,
    path: Sequence[str] = (),
    question: str | None = None,
) -> str:
    """En-tête contextuel attendu (ADR 0009 §3) : titre du document, chemin de
    sections (``" > "``) s'il n'est pas vide, question racine si l'unité en a."""
    lines = [title]
    if path:
        lines.append(" > ".join(path))
    if question is not None:
        lines.append(f"Question : {question}")
    return "\n".join(lines)


def with_header(head: str, body: str) -> str:
    return f"{head}\n\n{body}"


def no_pages() -> PageSpan:
    return PageSpan(None, None)


def make_unit(
    *blocks: Block,
    unit_type: UnitType = UnitType.SECTION,
    question: str | None = None,
    path: Sequence[str] = ("Cours",),
    ordinal: int = 0,
) -> KnowledgeUnit:
    """Unité construite à la main, indépendante du segmenteur."""
    return KnowledgeUnit(
        unit_id=UUID(int=100 + ordinal),
        ordinal=ordinal,
        unit_type=unit_type,
        question_text=question,
        section_path=SectionPath(tuple(path)),
        page_span=no_pages(),
        token_count=COUNTER.count(render_blocks(blocks)),
        blocks=tuple(blocks),
    )
