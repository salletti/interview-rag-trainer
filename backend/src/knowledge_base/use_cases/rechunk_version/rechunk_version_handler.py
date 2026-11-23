"""``RechunkVersion`` : lot de chunks d'une version pour un index d'une autre
``ChunkingPolicy`` (S9 ; ADR 0009 §7, ADR 0011 §7 ; knowledge-base.md §13,
O4).

Coupé en deux pour la work queue (S0 §5), comme ``RecordExtraction`` :

- ``prepare`` (hors transaction, rien d'écrit) : ``None`` sans re-parser si
  le document est retiré, la version n'est plus ``PUBLISHED``/``EXTRACTED``,
  l'index n'est plus vivant ou son indexation n'est plus ``PENDING`` ; sinon
  relecture du blob, re-parsing (O4 : aucun ``StructuredDocument`` persisté),
  segmentation et découpage selon la politique DE L'INDEX, budget compté par
  le tokenizer du PROFIL de l'index (``embedders.for_profile``), dans un
  thread.
- ``apply`` (transaction du work item) : revérification, puis le lot
  ``(version, politique)`` s'il n'existe pas encore (un lot n'est jamais
  réécrit, I7) et l'item ``index_version`` ; ``False`` sans aucune écriture
  si l'état a changé. La version, son état et son lot d'origine ne bougent
  pas ; les citations (version, section, pages) restent celles de la version.
- ``fail`` : l'indexation ``PENDING`` passe ``FAILED`` (couverture honnête :
  l'activation reste refusée, l'échec est visible et rejouable).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from knowledge_base.domain.entities.indexation import Indexation, IndexationStatus
from knowledge_base.domain.entities.knowledge_corpus import (
    IndexStatus,
    KnowledgeCorpus,
    SearchIndex,
)
from knowledge_base.domain.entities.knowledge_document import (
    DocumentStatus,
    DocumentVersion,
    KnowledgeDocument,
    VersionStatus,
)
from knowledge_base.domain.exceptions.corpus_errors import UnknownIndexError
from knowledge_base.domain.exceptions.document_errors import VersionNotFoundError
from knowledge_base.domain.providers.document_blob_reader import DocumentBlobReader
from knowledge_base.domain.providers.document_parser import DocumentParser
from knowledge_base.domain.providers.embedder_provider import EmbedderProvider
from knowledge_base.domain.providers.extraction_transaction import (
    RechunkTransaction,
)
from knowledge_base.domain.providers.indexing_transaction import (
    IndexingTransaction,
    IndexingUnitOfWork,
)
from knowledge_base.domain.value_objects.document_chunk import DocumentChunk
from knowledge_base.domain.value_objects.failure_reason import FailureReason
from knowledge_base.domain.value_objects.knowledge_unit import KnowledgeUnit
from knowledge_base.use_cases.extraction_steps import VersionExtractor
from knowledge_base.use_cases.indexing_steps import schedule_indexation
from knowledge_base.use_cases.rechunk_version.rechunk_version_command import (
    RechunkVersion,
)

logger = logging.getLogger(__name__)

_INDEXABLE_VERSIONS = frozenset({VersionStatus.EXTRACTED, VersionStatus.PUBLISHED})


@dataclass(frozen=True)
class PreparedRechunk:
    document_id: UUID
    version_id: UUID
    index_id: UUID
    # ``ChunkingPolicy.identity`` de l'index, persistée comme
    # ``chunking_policy_version`` du lot.
    policy_version: str
    units: tuple[KnowledgeUnit, ...]
    chunks: tuple[DocumentChunk, ...]


@dataclass(frozen=True)
class _Target:
    document: KnowledgeDocument
    version: DocumentVersion
    index: SearchIndex


class RechunkVersionHandler:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], IndexingUnitOfWork],
        blob_reader: DocumentBlobReader,
        parser: DocumentParser,
        embedders: EmbedderProvider,
        max_document_tokens: int | None = None,
    ) -> None:
        """``max_document_tokens`` : la borne de volume de l'extraction (O16),
        ``None`` pour n'en appliquer aucune."""
        self._unit_of_work = unit_of_work
        self._blob_reader = blob_reader
        self._parser = parser
        self._embedders = embedders
        self._max_document_tokens = max_document_tokens

    async def prepare(self, command: RechunkVersion) -> PreparedRechunk | None:
        """Tout le calcul, aucune écriture. Lève
        ``UnknownEmbeddingProfileError`` (profil de l'index non configuré),
        ``BlobMissingError``, ``DocumentParseError``, ``DocumentTooLargeError``
        ou ``ChunkBudgetError``."""
        async with self._unit_of_work() as uow:
            target = _target(
                command,
                await uow.documents.get(command.document_id),
                await uow.corpus.get_shared(),
                await uow.indexations.get(command.version_id, command.index_id),
            )
        if target is None:
            logger.info(
                "rechunking de la version %s pour l'index %s ignoré : document "
                "retiré, version, index ou indexation hors d'état",
                command.version_id,
                command.index_id,
            )
            return None
        extractor = VersionExtractor(
            parser=self._parser,
            token_counter=self._embedders.for_profile(
                target.index.profile
            ).token_counter,
            policy=target.index.chunking_policy,
            max_document_tokens=self._max_document_tokens,
        )
        content = await self._blob_reader.read(target.version.source.content_hash)
        units, chunks = await asyncio.to_thread(
            extractor.extract, content, target.document, target.version
        )
        return PreparedRechunk(
            document_id=command.document_id,
            version_id=command.version_id,
            index_id=command.index_id,
            policy_version=target.index.chunking_policy.identity,
            units=units,
            chunks=chunks,
        )

    async def apply(
        self, transaction: RechunkTransaction, prepared: PreparedRechunk
    ) -> bool:
        """Vrai si le lot existe (écrit ici ou déjà là) et que l'indexation est
        planifiée ; faux, sans aucune écriture, si l'état a changé depuis
        ``prepare``. Ne valide jamais la transaction."""
        command = RechunkVersion(
            prepared.document_id, prepared.version_id, prepared.index_id
        )
        target = _target(
            command,
            await transaction.documents.get(command.document_id),
            await transaction.corpus.get_shared(),
            await transaction.indexations.get(command.version_id, command.index_id),
        )
        if (
            target is None
            or target.index.chunking_policy.identity != prepared.policy_version
        ):
            logger.info(
                "lot de la version %s pour l'index %s non enregistré : état changé "
                "depuis le calcul",
                prepared.version_id,
                prepared.index_id,
            )
            return False
        existing = await transaction.chunks.list_chunks(
            prepared.version_id, prepared.policy_version
        )
        if not existing:
            await transaction.extractions.add_batch(
                prepared.version_id,
                prepared.policy_version,
                prepared.units,
                prepared.chunks,
            )
        await schedule_indexation(
            transaction.work_queue,
            prepared.document_id,
            prepared.version_id,
            prepared.index_id,
        )
        logger.info(
            "version %s rechunkée pour l'index %s : %d unités, %d chunks "
            "(politique %s%s)",
            prepared.version_id,
            prepared.index_id,
            len(prepared.units),
            len(prepared.chunks),
            prepared.policy_version,
            ", lot déjà présent" if existing else "",
        )
        return True

    async def fail(
        self,
        transaction: IndexingTransaction,
        command: RechunkVersion,
        reason: FailureReason,
    ) -> None:
        """L'indexation ``PENDING`` passe ``FAILED`` ; sans effet sinon (lever
        ferait boucler l'item sur son hook, O14)."""
        indexation = await transaction.indexations.get(
            command.version_id, command.index_id
        )
        if indexation is None or indexation.status is not IndexationStatus.PENDING:
            logger.info(
                "échec %s du rechunking (%s, %s) non enregistré : indexation "
                "absente ou plus PENDING",
                reason.code,
                command.version_id,
                command.index_id,
            )
            return
        indexation.fail(reason)
        await transaction.indexations.save(indexation)


def _target(
    command: RechunkVersion,
    document: KnowledgeDocument | None,
    corpus: KnowledgeCorpus | None,
    indexation: Indexation | None,
) -> _Target | None:
    version = _version(document, command.version_id)
    index = _index(corpus, command.index_id)
    if (
        document is None
        or version is None
        or index is None
        or indexation is None
        or document.status is not DocumentStatus.ACTIVE
        or version.status not in _INDEXABLE_VERSIONS
        or index.status is IndexStatus.RETIRED
        or indexation.status is not IndexationStatus.PENDING
    ):
        return None
    return _Target(document=document, version=version, index=index)


def _version(
    document: KnowledgeDocument | None, version_id: UUID
) -> DocumentVersion | None:
    if document is None:
        return None
    try:
        return document.get_version(version_id)
    except VersionNotFoundError:
        return None


def _index(corpus: KnowledgeCorpus | None, index_id: UUID) -> SearchIndex | None:
    if corpus is None:
        return None
    try:
        return corpus.get_index(index_id)
    except UnknownIndexError:
        return None
