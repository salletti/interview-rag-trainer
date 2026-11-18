"""Handler du ``kind`` ``extract_version`` : pont entre la work queue (S0 §5)
et ``RecordExtraction`` (knowledge-base.md §6).

1. ``use_case.prepare`` hors transaction : lecture du blob, parsing,
   segmentation et chunking dans un thread ; pendant ce temps, la boucle
   prolonge le lease (``ctx.heartbeat``) toutes les ``heartbeat_interval``.
2. UN ``ctx.finalize`` dont le callback appelle ``use_case.apply``, même s'il
   n'y a rien à extraire : l'item doit se terminer.

Erreurs terminales (sans retry) :

| code | cause | ``retryable`` de la version ``FAILED`` |
|---|---|---|
| ``EMPTY_DOCUMENT`` | aucun contenu | faux |
| ``INVALID_DOCUMENT`` | non UTF-8, type inconnu, conversion impossible | faux |
| ``DOCUMENT_TOO_COMPLEX`` | imbrication trop profonde pour le parseur | faux |
| ``CHUNKING_FAILED`` | ``ChunkBudgetError`` (budget sans place) | faux |
| ``BLOB_MISSING`` | original absent du volume | vrai (après restauration) |
| ``INVALID_PAYLOAD`` | payload de l'item illisible | — (aucune version désignée) |

Toute autre exception est transitoire pour S0 (retry avec backoff) ; à la
dernière tentative, la version passe ``FAILED`` avec ``retryable`` vrai.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
from typing import TYPE_CHECKING, ClassVar, Final, TypeVar
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_base.adapters.controller.tasks.retry_policy import TerminalWorkError
from knowledge_base.adapters.gateway.blob.filesystem_blob_store import (
    FileSystemBlobStore,
)
from knowledge_base.adapters.gateway.embedding.approximate_token_counter import (
    ApproximateTokenCounter,
)
from knowledge_base.adapters.gateway.parsing.docling_markdown_parser import (
    DoclingMarkdownParser,
)
from knowledge_base.adapters.gateway.parsing.plain_text_parser import PlainTextParser
from knowledge_base.adapters.gateway.parsing.routing_document_parser import (
    RoutingDocumentParser,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_extraction_transaction import (  # noqa: E501
    SqlAlchemyExtractionTransaction,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from knowledge_base.domain.exceptions.parsing_errors import (
    PARSE_ERROR_CODES,
    DocumentParseError,
)
from knowledge_base.domain.providers.document_blob_reader import BlobMissingError
from knowledge_base.domain.providers.document_parser import DocumentParser
from knowledge_base.domain.providers.extraction_transaction import (
    ExtractionTransaction,
)
from knowledge_base.domain.providers.token_counter import TokenCounter
from knowledge_base.domain.providers.work_queue import FailureInfo
from knowledge_base.domain.services.chunk_planner import ChunkBudgetError
from knowledge_base.domain.value_objects.chunking_policy import ChunkingPolicy
from knowledge_base.domain.value_objects.failure_reason import FailureReason
from knowledge_base.frameworks.configuration.chunking import DEFAULT_CHUNKING_POLICY
from knowledge_base.frameworks.configuration.settings import KnowledgeBaseSettings
from knowledge_base.use_cases.import_steps import EXTRACT_VERSION
from knowledge_base.use_cases.record_extraction.record_extraction_command import (
    RecordExtraction,
)
from knowledge_base.use_cases.record_extraction.record_extraction_handler import (
    PreparedExtraction,
    RecordExtractionHandler,
)

if TYPE_CHECKING:
    from knowledge_base.adapters.controller.tasks.execution import WorkContext
    from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
        ClaimedWorkItem,
    )

logger = logging.getLogger(__name__)

INGESTION_QUEUE: Final = "knowledge_base.ingestion"
BLOB_MISSING: Final = "BLOB_MISSING"
INVALID_PAYLOAD: Final = "INVALID_PAYLOAD"
CHUNKING_FAILED: Final = "CHUNKING_FAILED"
# Échecs déterministes : relancer l'extraction du même contenu n'a pas de sens.
# Tout code d'une DocumentParseError en fait partie.
NOT_RETRYABLE_CODES: Final = PARSE_ERROR_CODES | {CHUNKING_FAILED}

TransactionFactory = Callable[[AsyncSession], ExtractionTransaction]
T = TypeVar("T")


class ExtractVersionWorkHandler:
    kind: ClassVar[str] = EXTRACT_VERSION
    queue: ClassVar[str] = INGESTION_QUEUE
    max_attempts: ClassVar[int] = 5

    def __init__(
        self,
        use_case: RecordExtractionHandler,
        transaction: TransactionFactory,
        *,
        heartbeat_interval: timedelta | None = None,
    ) -> None:
        """``heartbeat_interval`` : ``None`` désactive les heartbeats (doubles
        de ``WorkContext`` des tests unitaires qui n'en ont pas)."""
        self._use_case = use_case
        self._transaction = transaction
        self._heartbeat_interval = heartbeat_interval

    async def execute(self, item: ClaimedWorkItem, ctx: WorkContext) -> None:
        command = _command(item.payload)
        try:
            prepared = await self._keeping_lease(ctx, self._use_case.prepare(command))
        except DocumentParseError as error:
            raise TerminalWorkError(
                error.code.value, error.message or str(error)
            ) from error
        except ChunkBudgetError as error:
            raise TerminalWorkError(CHUNKING_FAILED, str(error)) from error
        except BlobMissingError as error:
            raise TerminalWorkError(BLOB_MISSING, str(error)) from error

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
                "work item %s : payload illisible, aucun échec de version à "
                "enregistrer (%s)",
                item.id,
                error.code,
            )
            return
        reason = FailureReason(
            code=error.code,
            message=error.message,
            retryable=error.code not in NOT_RETRYABLE_CODES,
        )
        await self._use_case.fail(self._transaction(session), command, reason)

    async def _record(
        self, session: AsyncSession, prepared: PreparedExtraction
    ) -> None:
        applied = await self._use_case.apply(self._transaction(session), prepared)
        if applied:
            logger.info(
                "version %s extraite : %d unités, %d chunks (politique %s)",
                prepared.version_id,
                len(prepared.units),
                len(prepared.chunks),
                prepared.policy_version,
            )

    async def _keeping_lease(self, ctx: WorkContext, work: Awaitable[T]) -> T:
        """Attend ``work`` en prolongeant le lease. Un heartbeat refusé
        (``StaleAttempt``) est relevé dès la fin du travail, qui ne peut être
        interrompu (thread) : la tentative s'arrête sans finaliser."""
        if self._heartbeat_interval is None:
            return await work
        interval = self._heartbeat_interval.total_seconds()

        async def beat() -> None:
            while True:
                await asyncio.sleep(interval)
                await ctx.heartbeat()

        beating = asyncio.create_task(beat())
        try:
            result = await work
        finally:
            beating.cancel()
            # Relève le StaleAttempt d'un heartbeat refusé pendant le travail ;
            # l'annulation ci-dessus, elle, est attendue.
            with contextlib.suppress(asyncio.CancelledError):
                await beating
        return result


def _command(payload: Mapping[str, str]) -> RecordExtraction:
    try:
        return RecordExtraction(
            document_id=UUID(payload["document_id"]),
            version_id=UUID(payload["version_id"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise TerminalWorkError(
            INVALID_PAYLOAD, f"payload extract_version illisible : {dict(payload)!r}"
        ) from error


def build_extract_version_handler(
    session_factory: async_sessionmaker[AsyncSession],
    settings: KnowledgeBaseSettings,
    *,
    parser: DocumentParser | None = None,
    token_counter: TokenCounter | None = None,
    policy: ChunkingPolicy | None = None,
) -> ExtractVersionWorkHandler:
    """Composition de production ; chaque défaut est remplaçable (tests). Ne
    se connecte à rien et n'importe pas Docling (chargé à la première
    conversion)."""
    blobs = FileSystemBlobStore(settings.blob_root)
    use_case = RecordExtractionHandler(
        unit_of_work=lambda: SqlAlchemyUnitOfWork(session_factory),
        blob_reader=blobs,
        parser=parser or default_parser(),
        token_counter=token_counter or ApproximateTokenCounter(),
        policy=policy or DEFAULT_CHUNKING_POLICY,
    )
    return ExtractVersionWorkHandler(
        use_case=use_case,
        transaction=SqlAlchemyExtractionTransaction,
        heartbeat_interval=settings.heartbeat_interval,
    )


def default_parser() -> RoutingDocumentParser:
    return RoutingDocumentParser(
        {"text/markdown": DoclingMarkdownParser(), "text/plain": PlainTextParser()}
    )
