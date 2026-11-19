"""Handler du ``kind`` ``rechunk_version`` : pont entre la work queue (S0 §5)
et ``RechunkVersion`` (S9 ; knowledge-base.md §13, O4 ; ADR 0011 §7).

Même forme que ``extract_version`` : ``prepare`` hors transaction (blob,
re-parsing, découpage selon la politique de l'index), heartbeats pendant le
calcul (``keeping_lease``), puis UN ``ctx.finalize`` qui écrit le lot et
planifie ``index_version``.

Erreurs terminales (sans retry), codes du catalogue (S8) : ceux du parseur,
``DOCUMENT_TOO_LARGE``, ``CHUNKING_FAILED``, ``BLOB_MISSING`` comme pour
l'extraction ; ``COLLECTION_MISMATCH`` si le worker ne sait pas charger le
profil de l'index (son tokenizer mesure le budget) ; ``INVALID_PAYLOAD``. Le
hook d'échec fait passer l'indexation ``FAILED`` (``retryable`` d'après le
catalogue) : l'activation reste refusée et l'échec est visible et rejouable
par ``/failures`` (l'indexation repasse ``PENDING``, l'item est rouvert).
Toute autre exception est transitoire (backoff) ; à la dernière tentative,
le hook écrit l'échec.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import TYPE_CHECKING, ClassVar
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_base.adapters.controller.tasks.extract_version_handler import (
    INGESTION_QUEUE,
    INVALID_PAYLOAD,
    default_parser,
)
from knowledge_base.adapters.controller.tasks.lease import keeping_lease
from knowledge_base.adapters.controller.tasks.retry_policy import TerminalWorkError
from knowledge_base.adapters.gateway.blob.filesystem_blob_store import (
    FileSystemBlobStore,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_extraction_transaction import (  # noqa: E501
    SqlAlchemyExtractionTransaction,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from knowledge_base.domain.exceptions.parsing_errors import DocumentParseError
from knowledge_base.domain.providers.document_blob_reader import BlobMissingError
from knowledge_base.domain.providers.document_parser import DocumentParser
from knowledge_base.domain.providers.embedder_provider import (
    EmbedderProvider,
    UnknownEmbeddingProfileError,
)
from knowledge_base.domain.providers.extraction_transaction import (
    RechunkTransaction,
)
from knowledge_base.domain.providers.work_queue import FailureInfo
from knowledge_base.domain.services.chunk_planner import ChunkBudgetError
from knowledge_base.domain.value_objects.error_catalog import (
    BLOB_MISSING,
    CHUNKING_FAILED,
    COLLECTION_MISMATCH,
    DOCUMENT_TOO_LARGE,
    is_known_replayable,
)
from knowledge_base.domain.value_objects.failure_reason import FailureReason
from knowledge_base.frameworks.configuration.settings import KnowledgeBaseSettings
from knowledge_base.use_cases.indexing_steps import RECHUNK_VERSION
from knowledge_base.use_cases.rechunk_version.rechunk_version_command import (
    RechunkVersion,
)
from knowledge_base.use_cases.rechunk_version.rechunk_version_handler import (
    PreparedRechunk,
    RechunkVersionHandler,
)
from knowledge_base.use_cases.record_extraction.errors import DocumentTooLargeError

if TYPE_CHECKING:
    from knowledge_base.adapters.controller.tasks.execution import WorkContext
    from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
        ClaimedWorkItem,
    )

logger = logging.getLogger(__name__)

TransactionFactory = Callable[[AsyncSession], RechunkTransaction]


class RechunkVersionWorkHandler:
    kind: ClassVar[str] = RECHUNK_VERSION
    queue: ClassVar[str] = INGESTION_QUEUE
    max_attempts: ClassVar[int] = 5

    def __init__(
        self,
        use_case: RechunkVersionHandler,
        transaction: TransactionFactory,
        *,
        heartbeat_interval: timedelta | None = None,
    ) -> None:
        """``heartbeat_interval`` : ``None`` désactive les heartbeats (doubles
        de ``WorkContext`` sans heartbeat)."""
        self._use_case = use_case
        self._transaction = transaction
        self._heartbeat_interval = heartbeat_interval

    async def execute(self, item: ClaimedWorkItem, ctx: WorkContext) -> None:
        command = _command(item.payload)
        try:
            prepared = await keeping_lease(
                ctx, self._use_case.prepare(command), self._heartbeat_interval
            )
        except DocumentParseError as error:
            raise TerminalWorkError(
                error.code.value, error.message or str(error)
            ) from error
        except DocumentTooLargeError as error:
            raise TerminalWorkError(DOCUMENT_TOO_LARGE, str(error)) from error
        except ChunkBudgetError as error:
            raise TerminalWorkError(CHUNKING_FAILED, str(error)) from error
        except BlobMissingError as error:
            raise TerminalWorkError(BLOB_MISSING, str(error)) from error
        except UnknownEmbeddingProfileError as error:
            raise TerminalWorkError(COLLECTION_MISMATCH, str(error)) from error

        async def record(session: AsyncSession) -> None:
            if prepared is not None:
                await self._record(session, prepared)

        await ctx.finalize(record)

    async def on_terminal_failure(
        self, session: AsyncSession, item: ClaimedWorkItem, error: FailureInfo
    ) -> None:
        try:
            command = _command(item.payload)
        except TerminalWorkError:
            logger.warning(
                "work item %s : payload illisible, aucun échec d'indexation à "
                "enregistrer (%s)",
                item.id,
                error.code,
            )
            return
        reason = FailureReason(
            code=error.code,
            message=error.message,
            retryable=is_known_replayable(error.code),
        )
        await self._use_case.fail(self._transaction(session), command, reason)

    async def _record(self, session: AsyncSession, prepared: PreparedRechunk) -> None:
        await self._use_case.apply(self._transaction(session), prepared)


def _command(payload: Mapping[str, str]) -> RechunkVersion:
    try:
        return RechunkVersion(
            document_id=UUID(payload["document_id"]),
            version_id=UUID(payload["version_id"]),
            index_id=UUID(payload["index_id"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise TerminalWorkError(
            INVALID_PAYLOAD, f"payload rechunk_version illisible : {dict(payload)!r}"
        ) from error


def build_rechunk_version_handler(
    session_factory: async_sessionmaker[AsyncSession],
    settings: KnowledgeBaseSettings,
    *,
    embedders: EmbedderProvider,
    parser: DocumentParser | None = None,
) -> RechunkVersionWorkHandler:
    """Composition de production ; ne se connecte à rien, ne charge ni
    Docling ni modèle (le tokenizer du profil de l'index est chargé au premier
    comptage)."""
    use_case = RechunkVersionHandler(
        unit_of_work=lambda: SqlAlchemyUnitOfWork(session_factory),
        blob_reader=FileSystemBlobStore(settings.blob_root),
        parser=parser or default_parser(),
        embedders=embedders,
        max_document_tokens=settings.max_document_tokens,
    )
    return RechunkVersionWorkHandler(
        use_case=use_case,
        transaction=SqlAlchemyExtractionTransaction,
        heartbeat_interval=settings.heartbeat_interval,
    )
