"""``RecordExtraction`` : extraction d'une version ``RECEIVED`` en unités et
chunks (knowledge-base.md §3.4, §6, §7.3 ; I7, I13).

Le travail est coupé en deux pour la work queue (S0 §5) :

- ``prepare`` : lecture de l'agrégat (transaction brève, sans commit), du blob,
  puis parsing, segmentation et chunking dans un thread (``asyncio.to_thread``)
  pour que la boucle reste libre (heartbeats) ; n'écrit rien ;
- ``apply`` : dans la transaction de finalisation fournie par l'appelant,
  revérification métier (version toujours ``RECEIVED``, document ``ACTIVE``)
  puis ``EXTRACTED``, le lot d'unités et de chunks, et (S4) une
  ``Indexation PENDING`` et un item ``index_version`` par index vivant.

Une version supplantée ou un document retiré entre-temps est ignoré : il n'y a
plus rien à extraire, ce n'est pas une erreur.

Barrière blue/green (§13, ADR 0011 §5) : les index ``ACTIVE`` et
``BUILDING`` sont lus sous ``FOR SHARE`` de la ligne du corpus, dans la
transaction qui passe la version ``EXTRACTED`` ; une activation ne peut donc
pas s'intercaler entre l'extraction et la création de ses indexations.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from knowledge_base.domain.entities.indexation import Indexation
from knowledge_base.domain.entities.knowledge_document import (
    DocumentStatus,
    DocumentVersion,
    KnowledgeDocument,
    VersionStatus,
)
from knowledge_base.domain.exceptions.document_errors import VersionNotFoundError
from knowledge_base.domain.providers.document_blob_reader import DocumentBlobReader
from knowledge_base.domain.providers.document_parser import DocumentParser
from knowledge_base.domain.providers.document_reader import DocumentReader
from knowledge_base.domain.providers.extraction_transaction import (
    ExtractionTransaction,
)
from knowledge_base.domain.providers.token_counter import TokenCounter
from knowledge_base.domain.services.chunk_planner import ChunkPlanner
from knowledge_base.domain.services.knowledge_unit_segmenter import (
    KnowledgeUnitSegmenter,
)
from knowledge_base.domain.value_objects.chunking_policy import ChunkingPolicy
from knowledge_base.domain.value_objects.citation import Citation
from knowledge_base.domain.value_objects.document_chunk import DocumentChunk
from knowledge_base.domain.value_objects.failure_reason import FailureReason
from knowledge_base.domain.value_objects.knowledge_unit import KnowledgeUnit
from knowledge_base.domain.value_objects.section_path import SectionPath
from knowledge_base.use_cases.indexing_steps import schedule_indexation
from knowledge_base.use_cases.record_extraction.errors import DocumentTooLargeError
from knowledge_base.use_cases.record_extraction.record_extraction_command import (
    RecordExtraction,
)

logger = logging.getLogger(__name__)

# Code de journal d'une version passée ``EXTRACTED`` sans aucune indexation
# (aucun corpus, aucun index vivant, ou politique de chunking d'aucun index) :
# elle ne sera jamais publiée seule. Repère stable pour la réconciliation S8.
EXTRACTED_WITHOUT_INDEXATION: Final = "EXTRACTED_WITHOUT_INDEXATION"


@dataclass(frozen=True)
class PreparedExtraction:
    document_id: UUID
    version_id: UUID
    # ``ChunkingPolicy.identity`` (version + tokenizer), persistée comme
    # ``chunking_policy_version``.
    policy_version: str
    units: tuple[KnowledgeUnit, ...]
    chunks: tuple[DocumentChunk, ...]


class RecordExtractionHandler:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], DocumentReader],
        blob_reader: DocumentBlobReader,
        parser: DocumentParser,
        token_counter: TokenCounter,
        policy: ChunkingPolicy,
        max_document_tokens: int | None = None,
    ) -> None:
        """``max_document_tokens`` : borne du volume de texte extrait (O16),
        ``None`` pour n'en appliquer aucune."""
        if max_document_tokens is not None and max_document_tokens <= 0:
            raise ValueError(
                f"max_document_tokens doit être positif : {max_document_tokens}"
            )
        self._unit_of_work = unit_of_work
        self._blob_reader = blob_reader
        self._parser = parser
        self._segmenter = KnowledgeUnitSegmenter(token_counter, policy)
        self._planner = ChunkPlanner(token_counter, policy)
        self._policy = policy
        self._max_document_tokens = max_document_tokens

    async def prepare(self, command: RecordExtraction) -> PreparedExtraction | None:
        """Tout le calcul, aucune écriture. ``None`` s'il n'y a rien à
        extraire ; une ``DocumentParseError`` du parseur remonte telle quelle,
        ``DocumentTooLargeError`` si le texte dépasse la borne de volume."""
        async with self._unit_of_work() as uow:
            document = await uow.documents.get(command.document_id)
        version = _extractable_version(document, command.version_id)
        if document is None or version is None:
            logger.info(
                "extraction de la version %s ignorée : document absent ou retiré, "
                "ou version qui n'est plus RECEIVED",
                command.version_id,
            )
            return None
        content = await self._blob_reader.read(version.source.content_hash)
        units, chunks = await asyncio.to_thread(
            self._extract, content, document, version
        )
        return PreparedExtraction(
            document_id=document.document_id,
            version_id=version.version_id,
            policy_version=self._policy.identity,
            units=units,
            chunks=chunks,
        )

    async def apply(
        self, transaction: ExtractionTransaction, prepared: PreparedExtraction
    ) -> bool:
        """Vrai si la version est passée ``EXTRACTED`` avec son lot et ses
        indexations ; faux, sans aucune écriture, si elle a été supplantée ou
        le document retiré depuis ``prepare``."""
        document = await transaction.documents.get(prepared.document_id)
        if (
            document is None
            or _extractable_version(document, prepared.version_id) is None
        ):
            logger.info(
                "extraction de la version %s non enregistrée : état changé depuis "
                "le calcul",
                prepared.version_id,
            )
            return False
        document.record_extraction(prepared.version_id)
        await transaction.documents.save(document)
        await transaction.extractions.add_batch(
            prepared.version_id,
            prepared.policy_version,
            prepared.units,
            prepared.chunks,
        )
        if not await self._schedule_indexations(transaction, prepared):
            _log_extracted_without_indexation(prepared)
        return True

    async def _schedule_indexations(
        self, transaction: ExtractionTransaction, prepared: PreparedExtraction
    ) -> int:
        """Nombre d'indexations créées."""
        scheduled = 0
        corpus = await transaction.corpus.get_shared()
        for index in () if corpus is None else corpus.live_indexes():
            if index.chunking_policy.identity != prepared.policy_version:
                # Ce lot n'est pas celui de l'index : il lui faut une
                # ré-extraction avec sa propre politique (rechunking, S9).
                logger.warning(
                    "version %s non indexée dans %s : politique de l'index %s, lot %s",
                    prepared.version_id,
                    index.index_id,
                    index.chunking_policy.identity,
                    prepared.policy_version,
                )
                continue
            await transaction.indexations.add(
                Indexation.pending(
                    version_id=prepared.version_id,
                    index_id=index.index_id,
                    chunking_policy_version=prepared.policy_version,
                )
            )
            await schedule_indexation(
                transaction.work_queue,
                prepared.document_id,
                prepared.version_id,
                index.index_id,
            )
            scheduled += 1
        return scheduled

    async def fail(
        self,
        transaction: ExtractionTransaction,
        command: RecordExtraction,
        reason: FailureReason,
    ) -> None:
        """``RECEIVED`` -> ``FAILED`` ; sans effet si la version a changé
        d'état entre-temps (lever ferait boucler l'item sur son hook, O14)."""
        document = await transaction.documents.get(command.document_id)
        version = _version(document, command.version_id)
        if (
            document is None
            or version is None
            or version.status is not VersionStatus.RECEIVED
        ):
            logger.info(
                "échec %s de la version %s non enregistré : version absente ou "
                "plus RECEIVED",
                reason.code,
                command.version_id,
            )
            return
        document.fail_extraction(command.version_id, reason)
        await transaction.documents.save(document)

    def _extract(
        self, content: bytes, document: KnowledgeDocument, version: DocumentVersion
    ) -> tuple[tuple[KnowledgeUnit, ...], tuple[DocumentChunk, ...]]:
        """CPU pur, exécuté hors de la boucle asyncio."""
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


def _version(
    document: KnowledgeDocument | None, version_id: UUID
) -> DocumentVersion | None:
    if document is None:
        return None
    try:
        return document.get_version(version_id)
    except VersionNotFoundError:
        return None


def _log_extracted_without_indexation(prepared: PreparedExtraction) -> None:
    # Le code figure dans le message (format texte du processus) et en
    # attribut ``code`` de l'enregistrement (filtrage, futur export structuré).
    logger.warning(
        "code=%s document_id=%s version_id=%s policy_version=%s : version "
        "EXTRACTED sans aucune indexation, elle ne sera pas publiée",
        EXTRACTED_WITHOUT_INDEXATION,
        prepared.document_id,
        prepared.version_id,
        prepared.policy_version,
        extra={
            "code": EXTRACTED_WITHOUT_INDEXATION,
            "document_id": str(prepared.document_id),
            "version_id": str(prepared.version_id),
        },
    )


def _extractable_version(
    document: KnowledgeDocument | None, version_id: UUID
) -> DocumentVersion | None:
    """La version, si elle est encore à extraire : ``RECEIVED`` d'un document
    ``ACTIVE``."""
    version = _version(document, version_id)
    if document is None or version is None:
        return None
    if document.status is not DocumentStatus.ACTIVE:
        return None
    return version if version.status is VersionStatus.RECEIVED else None
