"""Handler du ``kind`` ``sync_visibility`` : pont entre la work queue (S0 §5)
et ``SyncDocumentVisibility`` (knowledge-base.md §6, §11).

Les effets sont tous dans Qdrant, convergents et rejouables ; la finalisation
n'écrit rien d'autre que le passage ``SUCCEEDED`` de l'item, sous fencing.
Toute erreur Qdrant est transitoire (retry avec backoff).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, ClassVar
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_base.adapters.controller.tasks.extract_version_handler import (
    INGESTION_QUEUE,
    INVALID_PAYLOAD,
)
from knowledge_base.adapters.controller.tasks.retry_policy import TerminalWorkError
from knowledge_base.adapters.gateway.persistence.sqlalchemy_unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from knowledge_base.domain.providers.vector_index import VectorIndex
from knowledge_base.use_cases.indexing_steps import SYNC_VISIBILITY
from knowledge_base.use_cases.sync_document_visibility.sync_document_visibility_command import (  # noqa: E501
    SyncDocumentVisibility,
)
from knowledge_base.use_cases.sync_document_visibility.sync_document_visibility_handler import (  # noqa: E501
    SyncDocumentVisibilityHandler,
)

if TYPE_CHECKING:
    from knowledge_base.adapters.controller.tasks.execution import WorkContext
    from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
        ClaimedWorkItem,
    )


class SyncVisibilityWorkHandler:
    kind: ClassVar[str] = SYNC_VISIBILITY
    queue: ClassVar[str] = INGESTION_QUEUE
    max_attempts: ClassVar[int] = 5

    def __init__(self, use_case: SyncDocumentVisibilityHandler) -> None:
        self._use_case = use_case

    async def execute(self, item: ClaimedWorkItem, ctx: WorkContext) -> None:
        await self._use_case.handle(_command(item.payload))

        async def nothing_to_write(_session: AsyncSession) -> None:
            return None

        await ctx.finalize(nothing_to_write)


def _command(payload: Mapping[str, str]) -> SyncDocumentVisibility:
    try:
        return SyncDocumentVisibility(
            document_id=UUID(payload["document_id"]),
            visibility_revision=int(payload["visibility_revision"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise TerminalWorkError(
            INVALID_PAYLOAD, f"payload sync_visibility illisible : {dict(payload)!r}"
        ) from error


def build_sync_visibility_handler(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    vector_index: VectorIndex,
) -> SyncVisibilityWorkHandler:
    """Composition de production ; ne se connecte à rien."""
    return SyncVisibilityWorkHandler(
        use_case=SyncDocumentVisibilityHandler(
            unit_of_work=lambda: SqlAlchemyUnitOfWork(session_factory),
            vector_index=vector_index,
        )
    )
