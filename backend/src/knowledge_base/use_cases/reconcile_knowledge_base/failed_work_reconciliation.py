"""Items ``FAILED`` dont l'état métier n'a pas suivi (S8 ; O14 ;
knowledge-base.md §5.2, §5.3, §10).

- ``extract_version`` ``FAILED`` et version encore ``RECEIVED``,
  ``index_version`` ou ``rechunk_version`` (S9) ``FAILED`` et indexation
  encore ``PENDING`` : le hook
  d'échec n'a pas abouti (``recover`` à la dernière tentative,
  ``TERMINAL_HOOK_FAILED``). L'échec métier est écrit à sa place, avec le code
  et le message de l'item et ``retryable`` d'après le catalogue des erreurs.
  L'item n'est JAMAIS rouvert : seul ``RetryIngestion`` rejoue une extraction
  ou une indexation (une erreur déterministe ne boucle pas).
- ``purge_document`` ``FAILED`` d'un document retiré non purgé : aucun hook
  métier ne la rejouerait ; l'item est rouvert, au plus
  ``max_reopenings`` fois au total (``reopen_count``), puis laissé au rejeu
  explicite et signalé à chaque cycle : Qdrant durablement en panne ne fait
  pas boucler la purge au rythme de la réconciliation.

Chaque correction verrouille l'item et vérifie qu'il est toujours ``FAILED``
(un rejeu concurrent l'a peut-être rouvert), puis relit l'agrégat.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Final

from knowledge_base.domain.entities.indexation import IndexationStatus
from knowledge_base.domain.entities.knowledge_document import VersionStatus
from knowledge_base.domain.exceptions.document_errors import VersionNotFoundError
from knowledge_base.domain.providers.failed_work import (
    WorkItemRecord,
    WorkItemStatus,
)
from knowledge_base.domain.providers.knowledge_document_repository import (
    ConcurrentModificationError,
)
from knowledge_base.domain.providers.reconciliation_store import (
    FailedItemFacts,
    ReconciliationStore,
    ReconciliationUnitOfWork,
)
from knowledge_base.domain.services.reconciliation import (
    DiscrepancyKind,
    FailedItemAction,
    decide_failed_item,
)
from knowledge_base.domain.value_objects.error_catalog import is_known_replayable
from knowledge_base.domain.value_objects.failure_reason import FailureReason
from knowledge_base.use_cases.import_steps import EXTRACT_VERSION
from knowledge_base.use_cases.indexing_steps import (
    INDEX_VERSION,
    PURGE_DOCUMENT,
    RECHUNK_VERSION,
)

logger = logging.getLogger(__name__)

WATCHED_KINDS: Final = (EXTRACT_VERSION, INDEX_VERSION, RECHUNK_VERSION, PURGE_DOCUMENT)

# Écrit l'échec métier dans la transaction de la correction ; faux si l'état
# ne s'y prête plus.
FailureWriter = Callable[[ReconciliationUnitOfWork, WorkItemRecord], Awaitable[bool]]


class FailedWorkReconciliation:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], ReconciliationUnitOfWork],
        store: ReconciliationStore,
        max_purge_reopenings: int,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._store = store
        self._max_purge_reopenings = max_purge_reopenings

    async def run(self) -> Counter[DiscrepancyKind]:
        counts: Counter[DiscrepancyKind] = Counter()
        for facts in await self._store.failed_items(WATCHED_KINDS):
            item = facts.item
            match self._decide(facts):
                case FailedItemAction.FAIL_VERSION:
                    if await self._adopt(item, self._fail_version):
                        counts[DiscrepancyKind.FAILED_WORK_WITHOUT_STATE] += 1
                case FailedItemAction.FAIL_INDEXATION:
                    if await self._adopt(item, self._fail_indexation):
                        counts[DiscrepancyKind.FAILED_WORK_WITHOUT_STATE] += 1
                case FailedItemAction.REOPEN_PURGE:
                    if await self._reopen_purge(item):
                        counts[DiscrepancyKind.FAILED_PURGE] += 1
                case FailedItemAction.KEEP_FAILED_PURGE:
                    counts[DiscrepancyKind.FAILED_PURGE] += 1
                    logger.warning(
                        "purge %s FAILED (%s) après %d réouverture(s) "
                        "automatique(s) : rejeu explicite attendu "
                        "(POST /api/v1/knowledge-base/failures/%s/retry)",
                        item.item_id,
                        item.last_error_code,
                        item.reopen_count,
                        item.item_id,
                    )
                case FailedItemAction.IGNORE:
                    pass
        return counts

    def _decide(self, facts: FailedItemFacts) -> FailedItemAction:
        return decide_failed_item(
            facts.item.kind,
            version_status=facts.version_status,
            indexation_status=facts.indexation_status,
            document_status=facts.document_status,
            purged=facts.purged,
            live_work=facts.live_work,
            reopen_count=facts.item.reopen_count,
            max_reopenings=self._max_purge_reopenings,
        )

    async def _adopt(
        self,
        item: WorkItemRecord,
        write: FailureWriter,
    ) -> bool:
        """Écrit l'échec métier que le hook n'a pas écrit ; vrai s'il l'a été."""
        try:
            async with self._unit_of_work() as uow:
                current = await uow.failed_work.get_for_update(item.item_id)
                if current is None or current.status is not WorkItemStatus.FAILED:
                    return False
                if not await write(uow, current):
                    return False
                await uow.commit()
        except ConcurrentModificationError as conflict:
            logger.info(
                "échec de l'item %s non adopté, document modifié entre-temps : %s",
                item.item_id,
                conflict,
            )
            return False
        logger.warning(
            "item %s (%s) FAILED sans état métier (%s) : échec écrit par la "
            "réconciliation (O14)",
            item.item_id,
            item.kind,
            item.last_error_code,
        )
        return True

    @staticmethod
    async def _fail_version(
        uow: ReconciliationUnitOfWork, item: WorkItemRecord
    ) -> bool:
        document_id = item.payload_id("document_id")
        version_id = item.payload_id("version_id")
        reason = _reason(item)
        if document_id is None or version_id is None or reason is None:
            return False
        document = await uow.documents.get(document_id)
        if document is None:
            return False
        try:
            version = document.get_version(version_id)
        except VersionNotFoundError:
            return False
        if version.status is not VersionStatus.RECEIVED:
            return False
        document.fail_extraction(version_id, reason)
        await uow.documents.save(document)
        return True

    @staticmethod
    async def _fail_indexation(
        uow: ReconciliationUnitOfWork, item: WorkItemRecord
    ) -> bool:
        version_id = item.payload_id("version_id")
        index_id = item.payload_id("index_id")
        reason = _reason(item)
        if version_id is None or index_id is None or reason is None:
            return False
        indexation = await uow.indexations.get(version_id, index_id)
        if indexation is None or indexation.status is not IndexationStatus.PENDING:
            return False
        indexation.fail(reason)
        await uow.indexations.save(indexation)
        return True

    async def _reopen_purge(self, item: WorkItemRecord) -> bool:
        """La borne est relue sous le verrou : le constat (``item``) a pu être
        dépassé par un rejeu explicite, puis un nouvel échec, entre-temps."""
        async with self._unit_of_work() as uow:
            current = await uow.failed_work.get_for_update(item.item_id)
            if (
                current is None
                or current.status is not WorkItemStatus.FAILED
                or current.reopen_count >= self._max_purge_reopenings
            ):
                return False
            await uow.failed_work.reopen(item.item_id)
            await uow.commit()
        logger.warning(
            "purge %s FAILED (%s) rouverte par la réconciliation (réouverture %d/%d)",
            item.item_id,
            current.last_error_code,
            current.reopen_count + 1,
            self._max_purge_reopenings,
        )
        return True


def _reason(item: WorkItemRecord) -> FailureReason | None:
    code = item.last_error_code
    if not code:
        return None
    return FailureReason(
        code=code,
        message=item.last_error_message or code,
        retryable=is_known_replayable(code),
    )
