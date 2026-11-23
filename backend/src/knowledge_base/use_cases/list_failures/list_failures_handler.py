"""``ListFailures`` : consultation des items ``FAILED`` (S8 ; ADR 0004 point 11 :
pas de DLQ RabbitMQ en V1, l'état ``FAILED`` en base joue le rôle de
dead-letter, consultable et relançable ; O10).

Du plus récent au plus ancien. ``replayable`` dit si ``RetryIngestion``
rouvrira l'item (catalogue des erreurs) ; il ne dit pas si l'état métier le
permet encore (version supplantée, document retiré), ce que le rejeu
revérifie. Les cycles de réconciliation ``FAILED`` ne sont pas listés : ils
ne se rejouent pas, le cycle suivant les remplace (leur échec reste en base
et dans les journaux du worker).
"""

from __future__ import annotations

from collections.abc import Callable

from knowledge_base.domain.providers.failed_work import (
    FailedWorkUnitOfWork,
    WorkItemRecord,
)
from knowledge_base.domain.value_objects.error_catalog import is_known_replayable
from knowledge_base.use_cases.list_failures.failure_view import FailureView
from knowledge_base.use_cases.list_failures.list_failures_command import ListFailures
from knowledge_base.use_cases.reconcile_knowledge_base.reconcile_knowledge_base_command import (  # noqa: E501
    RECONCILE_KNOWLEDGE_BASE,
)


class ListFailuresHandler:
    def __init__(self, *, unit_of_work: Callable[[], FailedWorkUnitOfWork]) -> None:
        self._unit_of_work = unit_of_work

    async def handle(self, query: ListFailures) -> tuple[FailureView, ...]:
        async with self._unit_of_work() as uow:
            records = await uow.failed_work.list_failed(
                query.limit, excluding_kinds=(RECONCILE_KNOWLEDGE_BASE,)
            )
        return tuple(_view(record) for record in records)


def _view(record: WorkItemRecord) -> FailureView:
    return FailureView(
        item_id=record.item_id,
        kind=record.kind,
        code=record.last_error_code,
        message=record.last_error_message,
        attempts=record.attempts,
        max_attempts=record.max_attempts,
        failed_at=record.failed_at,
        replayable=is_known_replayable(record.last_error_code),
        document_id=record.payload_id("document_id"),
        version_id=record.payload_id("version_id"),
        index_id=record.payload_id("index_id"),
    )
