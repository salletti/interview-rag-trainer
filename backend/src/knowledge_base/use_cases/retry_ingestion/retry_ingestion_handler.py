"""``RetryIngestion`` : rejeu explicite d'un item ``FAILED`` (S8 ;
knowledge-base.md §5.2, §5.3, §6 ; ADR 0004 points 10 et 11 ; S0 §3,
transition ``reopen``).

L'item est ROUVERT, jamais remplacé (``dedup_key`` unique pour toujours) :
``PENDING``, tentatives à zéro, dernière erreur conservée, jeton de fencing
jamais reculé. Dans la MÊME transaction, l'état métier qu'il désigne est
remis en attente :

- ``extract_version`` : version ``FAILED`` -> ``RECEIVED`` (cause effacée) ;
  une version encore ``RECEIVED`` (hook d'échec jamais exécuté, O14) est
  laissée telle quelle ;
- ``index_version`` : indexation ``FAILED`` -> ``PENDING`` ; la version reste
  ``EXTRACTED`` (§5.2), une indexation encore ``PENDING`` est laissée ;
- ``purge_document`` et les autres kinds : l'item seul.

Refus, sans aucune écriture : item inconnu, item qui n'est pas ``FAILED``
(dont un rejeu concurrent déjà passé : la ligne de l'item est verrouillée,
le second rejeu attend le premier et le voit), échec que le catalogue dit
non rejouable ou ne connaît pas, travail sans objet (version supplantée,
document retiré, index retiré, indexation annulée).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from knowledge_base.domain.entities.indexation import IndexationStatus
from knowledge_base.domain.entities.knowledge_corpus import IndexStatus
from knowledge_base.domain.entities.knowledge_document import (
    DocumentStatus,
    DocumentVersion,
    KnowledgeDocument,
    VersionStatus,
)
from knowledge_base.domain.exceptions.corpus_errors import UnknownIndexError
from knowledge_base.domain.exceptions.document_errors import VersionNotFoundError
from knowledge_base.domain.providers.failed_work import (
    FailedWorkUnitOfWork,
    WorkItemRecord,
)
from knowledge_base.domain.value_objects.error_catalog import replayable_or_not_known
from knowledge_base.use_cases.import_steps import EXTRACT_VERSION
from knowledge_base.use_cases.indexing_steps import INDEX_VERSION, PURGE_DOCUMENT
from knowledge_base.use_cases.retry_ingestion.errors import (
    FailureNotFoundError,
    ItemNotFailedError,
    NotReplayableError,
    ObsoleteWorkError,
)
from knowledge_base.use_cases.retry_ingestion.retry_ingestion_command import (
    RetryIngestion,
)
from knowledge_base.use_cases.retry_ingestion.retry_ingestion_result import (
    RetryIngestionResult,
)

logger = logging.getLogger(__name__)

_FAILED = "FAILED"
_INDEXABLE_VERSIONS = frozenset({VersionStatus.EXTRACTED, VersionStatus.PUBLISHED})


class RetryIngestionHandler:
    def __init__(self, *, unit_of_work: Callable[[], FailedWorkUnitOfWork]) -> None:
        self._unit_of_work = unit_of_work

    async def handle(self, command: RetryIngestion) -> RetryIngestionResult:
        async with self._unit_of_work() as uow:
            item = await uow.failed_work.get_for_update(command.item_id)
            if item is None:
                raise FailureNotFoundError(command.item_id)
            if item.status != _FAILED:
                raise ItemNotFailedError(item.item_id, item.status)
            if not replayable_or_not_known(item.last_error_code):
                raise NotReplayableError(item.item_id, item.last_error_code or "")
            if item.kind == EXTRACT_VERSION:
                await _reset_extraction(uow, item)
            elif item.kind == INDEX_VERSION:
                await _reset_indexation(uow, item)
            elif item.kind == PURGE_DOCUMENT:
                await _ensure_withdrawn(uow, item)
            # Ligne verrouillée par get_for_update et encore FAILED : la
            # réouverture ne peut pas être refusée.
            await uow.failed_work.reopen(item.item_id)
            await uow.commit()
        logger.info(
            "item %s (%s) rouvert après l'échec %s (tentative %d/%d)",
            item.item_id,
            item.kind,
            item.last_error_code,
            item.attempts,
            item.max_attempts,
        )
        return RetryIngestionResult(item_id=item.item_id, kind=item.kind)


async def _reset_extraction(uow: FailedWorkUnitOfWork, item: WorkItemRecord) -> None:
    document, version = await _document_and_version(uow, item)
    if version.status is VersionStatus.FAILED:
        document.retry_extraction(version.version_id)
        await uow.documents.save(document)
    elif version.status is not VersionStatus.RECEIVED:
        raise ObsoleteWorkError(item.item_id, _version_reason(version))


async def _reset_indexation(uow: FailedWorkUnitOfWork, item: WorkItemRecord) -> None:
    _, version = await _document_and_version(uow, item)
    if version.status not in _INDEXABLE_VERSIONS:
        raise ObsoleteWorkError(item.item_id, _version_reason(version))
    index_id = item.payload_id("index_id")
    corpus = await uow.corpus.get_shared()
    if index_id is None or corpus is None:
        raise ObsoleteWorkError(item.item_id, "index inconnu")
    try:
        index = corpus.get_index(index_id)
    except UnknownIndexError:
        raise ObsoleteWorkError(item.item_id, "index inconnu") from None
    if index.status is IndexStatus.RETIRED:
        raise ObsoleteWorkError(item.item_id, "index retiré")
    indexation = await uow.indexations.get(version.version_id, index_id)
    if indexation is None or indexation.status is IndexationStatus.CANCELLED:
        raise ObsoleteWorkError(item.item_id, "indexation annulée")
    if indexation.status is IndexationStatus.COMPLETED:
        raise ObsoleteWorkError(item.item_id, "indexation déjà COMPLETED")
    if indexation.status is IndexationStatus.FAILED:
        indexation.retry()
        await uow.indexations.save(indexation)


async def _ensure_withdrawn(uow: FailedWorkUnitOfWork, item: WorkItemRecord) -> None:
    document_id = item.payload_id("document_id")
    document = None if document_id is None else await uow.documents.get(document_id)
    if document is None or document.status is not DocumentStatus.WITHDRAWN:
        raise ObsoleteWorkError(item.item_id, "aucun document retiré à purger")


async def _document_and_version(
    uow: FailedWorkUnitOfWork, item: WorkItemRecord
) -> tuple[KnowledgeDocument, DocumentVersion]:
    document_id = item.payload_id("document_id")
    version_id = item.payload_id("version_id")
    document = None if document_id is None else await uow.documents.get(document_id)
    if document is None or version_id is None:
        raise ObsoleteWorkError(item.item_id, "version inconnue")
    if document.status is DocumentStatus.WITHDRAWN:
        raise ObsoleteWorkError(item.item_id, "document retiré")
    try:
        return document, document.get_version(version_id)
    except VersionNotFoundError:
        raise ObsoleteWorkError(item.item_id, "version inconnue") from None


def _version_reason(version: DocumentVersion) -> str:
    if version.status in {VersionStatus.DISCARDED, VersionStatus.SUPERSEDED}:
        return f"version supplantée ({version.status})"
    return f"version déjà {version.status}"
