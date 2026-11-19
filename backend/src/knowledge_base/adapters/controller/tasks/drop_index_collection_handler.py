"""Handler du ``kind`` ``drop_index_collection`` : pont entre la work queue
(S0 §5) et ``DropIndexCollection`` (S9 ; knowledge-base.md §13).

L'item est planifié par l'activation d'un nouvel index, disponible après le
délai ``KB_INDEX_DROP_DELAY_SECONDS`` (une recherche commencée avant la
bascule peut encore lire l'ancienne collection). La suppression a lieu hors
transaction ; la finalisation n'écrit rien d'autre que la fin de l'item.

Erreurs : index encore ``ACTIVE`` ou ``BUILDING`` → terminale
``INDEX_STILL_LIVE`` (non rejouable : la collection d'un index vivant n'est
jamais supprimée) ; payload illisible ou index inconnu → terminale
``INVALID_PAYLOAD`` ; panne de Qdrant ou de PostgreSQL → transitoire. Aucun
état métier n'enregistre l'échec : l'item ``FAILED`` reste consultable et,
pour une panne, rejouable (``/failures``).
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
from knowledge_base.domain.exceptions.corpus_errors import UnknownIndexError
from knowledge_base.domain.providers.index_collections import IndexCollections
from knowledge_base.domain.value_objects.error_catalog import INDEX_STILL_LIVE
from knowledge_base.use_cases.drop_index_collection.drop_index_collection_command import (  # noqa: E501
    DropIndexCollection,
)
from knowledge_base.use_cases.drop_index_collection.drop_index_collection_handler import (  # noqa: E501
    DropIndexCollectionHandler,
)
from knowledge_base.use_cases.drop_index_collection.errors import (
    IndexStillLiveError,
)
from knowledge_base.use_cases.indexing_steps import DROP_INDEX_COLLECTION

if TYPE_CHECKING:
    from knowledge_base.adapters.controller.tasks.execution import WorkContext
    from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
        ClaimedWorkItem,
    )


class DropIndexCollectionWorkHandler:
    kind: ClassVar[str] = DROP_INDEX_COLLECTION
    queue: ClassVar[str] = INGESTION_QUEUE
    max_attempts: ClassVar[int] = 5

    def __init__(self, use_case: DropIndexCollectionHandler) -> None:
        self._use_case = use_case

    async def execute(self, item: ClaimedWorkItem, ctx: WorkContext) -> None:
        command = _command(item.payload)
        try:
            await self._use_case.handle(command)
        except IndexStillLiveError as error:
            raise TerminalWorkError(INDEX_STILL_LIVE, str(error)) from error
        except UnknownIndexError as error:
            raise TerminalWorkError(INVALID_PAYLOAD, str(error)) from error

        async def nothing_to_write(_session: AsyncSession) -> None:
            return None

        await ctx.finalize(nothing_to_write)


def _command(payload: Mapping[str, str]) -> DropIndexCollection:
    try:
        return DropIndexCollection(index_id=UUID(payload["index_id"]))
    except (KeyError, TypeError, ValueError) as error:
        raise TerminalWorkError(
            INVALID_PAYLOAD,
            f"payload drop_index_collection illisible : {dict(payload)!r}",
        ) from error


def build_drop_index_collection_handler(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    collections: IndexCollections,
) -> DropIndexCollectionWorkHandler:
    """Composition de production ; ne se connecte à rien."""
    return DropIndexCollectionWorkHandler(
        DropIndexCollectionHandler(
            unit_of_work=lambda: SqlAlchemyUnitOfWork(session_factory),
            collections=collections,
        )
    )
