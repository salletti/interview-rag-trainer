"""Exécution d'un work item : claim, handler hors transaction, finalisation
sous fencing (S0 §5).

```text
T1  claim                → COMMIT   (None → fin silencieuse)
    handler.execute               hors transaction, heartbeats éventuels
T2  ctx.finalize          → COMMIT   écritures métier + complete(id, token)
en cas d'exception :
T2' retry_later | fail    → COMMIT   (+ hook d'échec terminal dans la même transaction)
```
"""

from __future__ import annotations

import logging
import os
import random
import socket
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_base.adapters.controller.tasks.registry import (
    HandlesTerminalFailure,
    WorkHandler,
)
from knowledge_base.adapters.controller.tasks.resources import (
    KnowledgeBaseWorkerResources,
)
from knowledge_base.adapters.controller.tasks.retry_policy import (
    StaleAttempt,
    TerminalWorkError,
    TransientWorkError,
    classify_error,
    compute_backoff,
)
from knowledge_base.adapters.failure_policy import (
    FailureAction,
    FailureDecision,
    has_attempts_left,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    ClaimedWorkItem,
    SqlAlchemyWorkLifecycle,
)
from knowledge_base.domain.providers.work_queue import (
    NOT_FINALIZED,
    TERMINAL_HOOK_FAILED,
    FailureInfo,
)
from knowledge_base.frameworks.configuration.settings import KnowledgeBaseSettings

logger = logging.getLogger(__name__)

WorkOutcome = Literal["skipped", "succeeded", "retry_scheduled", "failed", "stale"]
FinalizeCallback = Callable[[AsyncSession], Awaitable[None]]


class WorkContext:
    """Ce que la tentative courante peut faire sur son item."""

    def __init__(
        self,
        item: ClaimedWorkItem,
        lifecycle: SqlAlchemyWorkLifecycle,
        resources: KnowledgeBaseWorkerResources,
        settings: KnowledgeBaseSettings,
    ) -> None:
        self._item = item
        self._lifecycle = lifecycle
        self._resources = resources
        self._lease_ttl = settings.lease_ttl
        self.heartbeat_interval: timedelta = settings.heartbeat_interval
        self.finalized = False

    async def heartbeat(self) -> None:
        """Prolonge le lease. Lève ``StaleAttempt`` si la tentative n'est plus
        la tentative courante : le handler doit s'arrêter au plus tôt."""
        item = self._item
        if not await self._lifecycle.heartbeat(
            item.id, item.fencing_token, self._lease_ttl
        ):
            raise StaleAttempt(f"heartbeat refusé pour {item.id}")

    async def finalize(self, callback: FinalizeCallback) -> None:
        """Écritures métier de ``callback`` et passage ``SUCCEEDED`` dans une
        seule transaction. Si le token n'est plus courant, toute la transaction
        est annulée et ``StaleAttempt`` est levée."""
        item = self._item
        async with self._resources.session_factory() as session, session.begin():
            await callback(session)
            if not await self._lifecycle.complete(session, item.id, item.fencing_token):
                raise StaleAttempt(f"finalisation refusée pour {item.id}")
        self.finalized = True


def default_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


async def execute_work_item(
    item_id: UUID,
    resources: KnowledgeBaseWorkerResources,
    settings: KnowledgeBaseSettings,
    *,
    owner: str | None = None,
) -> WorkOutcome:
    lifecycle = SqlAlchemyWorkLifecycle(resources.session_factory)
    item = await lifecycle.claim(
        item_id,
        owner or default_owner(),
        settings.lease_ttl,
        max_attempts_by_kind=resources.registry.max_attempts_by_kind(),
    )
    if item is None:
        logger.info("work item %s non réclamable (doublon, terminé ou absent)", item_id)
        return "skipped"

    ctx = WorkContext(item, lifecycle, resources, settings)
    handler: WorkHandler | None = None
    try:
        handler = resources.registry.get(item.kind)
        await handler.execute(item, ctx)
        if not ctx.finalized:
            raise TerminalWorkError(
                NOT_FINALIZED,
                f"le handler {type(handler).__name__} s'est terminé sans "
                "appeler ctx.finalize",
            )
    except Exception as error:
        decision = classify_error(
            error, attempts=item.attempts, max_attempts=item.max_attempts
        )
        return await _apply_failure(
            item, handler, decision, error, lifecycle, resources, settings
        )
    logger.info("work item %s (%s) terminé", item.id, item.kind)
    return "succeeded"


async def _apply_failure(
    item: ClaimedWorkItem,
    handler: WorkHandler | None,
    decision: FailureDecision,
    error: Exception,
    lifecycle: SqlAlchemyWorkLifecycle,
    resources: KnowledgeBaseWorkerResources,
    settings: KnowledgeBaseSettings,
) -> WorkOutcome:
    failure = decision.failure
    if decision.action == "ignore" or failure is None:
        logger.info(
            "work item %s : tentative %s périmée (%s)",
            item.id,
            item.fencing_token,
            error,
        )
        return "stale"

    log_failed_attempt(item, failure, decision.action, error)
    if decision.action == "retry":
        return await _retry_later(item, failure, lifecycle, settings)
    hook = handler if isinstance(handler, HandlesTerminalFailure) else None
    try:
        recorded = await _fail(item, failure, hook, lifecycle, resources)
    except Exception as hook_error:
        return await _after_hook_failure(
            item, failure, hook_error, lifecycle, resources, settings
        )
    if recorded:
        return "failed"
    logger.info("work item %s : échec non enregistré, tentative périmée", item.id)
    return "stale"


def log_failed_attempt(
    item: ClaimedWorkItem,
    failure: FailureInfo,
    action: FailureAction,
    error: Exception,
) -> None:
    """Un échec métier attendu (``TerminalWorkError``, code du catalogue :
    ``OCR_REQUIRED``, ``EMPTY_DOCUMENT``…) tient en une ligne, sans trace : sa
    cause est dans son code et son message, et la trace chaînée de l'erreur
    d'origine n'apprend rien sur un cas normal. Une erreur transitoire
    déclarée reste un WARNING tracé ; une erreur inattendue, un ERROR tracé."""
    if isinstance(error, TerminalWorkError):
        # %r : un message de parseur peut contenir des retours à la ligne.
        logger.warning(
            "work item %s (%s) tentative %s/%s : %s → %s : %r",
            item.id,
            item.kind,
            item.attempts,
            item.max_attempts,
            failure.code,
            action,
            failure.message,
        )
        return
    level = logging.WARNING if isinstance(error, TransientWorkError) else logging.ERROR
    logger.log(
        level,
        "work item %s (%s) tentative %s/%s : %s → %s",
        item.id,
        item.kind,
        item.attempts,
        item.max_attempts,
        failure.code,
        action,
        exc_info=error,
    )


async def _retry_later(
    item: ClaimedWorkItem,
    failure: FailureInfo,
    lifecycle: SqlAlchemyWorkLifecycle,
    settings: KnowledgeBaseSettings,
) -> WorkOutcome:
    delay = compute_backoff(
        item.attempts,
        base=settings.backoff_base,
        cap=settings.backoff_cap,
        uniform=random.uniform,
    )
    if await lifecycle.retry_later(item.id, item.fencing_token, failure, delay):
        return "retry_scheduled"
    logger.info("work item %s : retry non enregistré, tentative périmée", item.id)
    return "stale"


async def _fail(
    item: ClaimedWorkItem,
    failure: FailureInfo,
    hook: HandlesTerminalFailure | None,
    lifecycle: SqlAlchemyWorkLifecycle,
    resources: KnowledgeBaseWorkerResources,
) -> bool:
    """``fail`` et le hook métier dans la même transaction ; le hook n'est
    appelé que si l'item était encore la tentative courante. Une exception du
    hook annule toute la transaction et remonte à l'appelant."""
    async with resources.session_factory() as session, session.begin():
        if not await lifecycle.fail(session, item.id, item.fencing_token, failure):
            return False
        if hook is not None:
            await hook.on_terminal_failure(session, item, failure)
    return True


async def _after_hook_failure(
    item: ClaimedWorkItem,
    failure: FailureInfo,
    hook_error: Exception,
    lifecycle: SqlAlchemyWorkLifecycle,
    resources: KnowledgeBaseWorkerResources,
    settings: KnowledgeBaseSettings,
) -> WorkOutcome:
    """Le hook n'a pas pu écrire l'échec métier : nouvelle tentative tant qu'il
    en reste (l'item repasse par le handler puis le hook), sinon ``FAILED``
    sans hook. L'état métier de ce dernier cas est le point ouvert O14."""
    logger.error(
        "work item %s : hook d'échec terminal en erreur après %s",
        item.id,
        failure.code,
        exc_info=hook_error,
    )
    hook_failure = FailureInfo(
        TERMINAL_HOOK_FAILED,
        f"{failure.code} ; hook en erreur : {type(hook_error).__name__}: {hook_error}",
    )
    if has_attempts_left(attempts=item.attempts, max_attempts=item.max_attempts):
        return await _retry_later(item, hook_failure, lifecycle, settings)
    if await _fail(item, hook_failure, None, lifecycle, resources):
        return "failed"
    return "stale"
