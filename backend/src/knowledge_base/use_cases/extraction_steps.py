"""Parsing, segmentation et découpage d'une version, communs à l'extraction
(``RecordExtraction``, S3) et au rechunking pour un index d'une autre
politique (``RechunkVersion``, S9 ; O4 : l'original est re-parsé, aucun
``StructuredDocument`` n'est persisté).

CPU pur, sans entrée-sortie : les handlers l'exécutent dans un thread
(``asyncio.to_thread``) pour laisser la boucle envoyer les heartbeats.
"""

from __future__ import annotations

from uuid import UUID

from knowledge_base.domain.entities.knowledge_document import (
    DocumentVersion,
    KnowledgeDocument,
)
from knowledge_base.domain.providers.document_parser import DocumentParser
from knowledge_base.domain.providers.token_counter import TokenCounter
from knowledge_base.domain.services.chunk_planner import ChunkPlanner
from knowledge_base.domain.services.knowledge_unit_segmenter import (
    KnowledgeUnitSegmenter,
)
from knowledge_base.domain.value_objects.chunking_policy import ChunkingPolicy
from knowledge_base.domain.value_objects.citation import Citation
from knowledge_base.domain.value_objects.document_chunk import DocumentChunk
from knowledge_base.domain.value_objects.knowledge_unit import KnowledgeUnit
from knowledge_base.domain.value_objects.section_path import SectionPath
from knowledge_base.use_cases.record_extraction.errors import DocumentTooLargeError


class VersionExtractor:
    """Une politique et le compteur de tokens qui mesure son budget : le lot
    produit est celui de ``policy.identity``."""

    def __init__(
        self,
        *,
        parser: DocumentParser,
        token_counter: TokenCounter,
        policy: ChunkingPolicy,
        max_document_tokens: int | None,
    ) -> None:
        """``max_document_tokens`` : borne du volume de texte extrait (O16),
        ``None`` pour n'en appliquer aucune."""
        if max_document_tokens is not None and max_document_tokens <= 0:
            raise ValueError(
                f"max_document_tokens doit être positif : {max_document_tokens}"
            )
        self._parser = parser
        self._segmenter = KnowledgeUnitSegmenter(token_counter, policy)
        self._planner = ChunkPlanner(token_counter, policy)
        self._max_document_tokens = max_document_tokens

    def extract(
        self, content: bytes, document: KnowledgeDocument, version: DocumentVersion
    ) -> tuple[tuple[KnowledgeUnit, ...], tuple[DocumentChunk, ...]]:
        """Une ``DocumentParseError`` du parseur remonte telle quelle ;
        ``DocumentTooLargeError`` si le texte dépasse la borne de volume,
        ``ChunkBudgetError`` si le budget ne laisse aucune place."""
        structured = self._parser.parse(
            content, version.source.media_type, version.source.filename
        )
        units = tuple(
            self._segmenter.segment(
                structured, kind=document.kind, version_id=version.version_id
            )
        )
        self._ensure_within_volume(version.version_id, units)
        citation = Citation(
            document_id=document.document_id,
            document_title=document.title,
            version_id=version.version_id,
            version_number=version.number,
            section_path=SectionPath(),
            page_start=None,
            page_end=None,
            source_filename=version.source.filename,
        )
        chunks = tuple(c for unit in units for c in self._planner.plan(unit, citation))
        return units, chunks

    def _ensure_within_volume(
        self, version_id: UUID, units: tuple[KnowledgeUnit, ...]
    ) -> None:
        """O16 : avant le chunking et toute écriture. La mesure est le texte
        des unités, sans les en-têtes contextuels que chaque chunk répète."""
        if self._max_document_tokens is None:
            return
        tokens = sum(unit.token_count for unit in units)
        if tokens > self._max_document_tokens:
            raise DocumentTooLargeError(version_id, tokens, self._max_document_tokens)
