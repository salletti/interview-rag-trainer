"""Registre ``kind`` → handler de travail (S0 §8.2).

Les handlers de production sont composés par ``build_work_handlers``
(``adapters/controller/dependencies.py``) ; depuis S3 : ``extract_version``.
Les handlers de test sont injectés par les tests, jamais déclarés ici.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import TYPE_CHECKING, ClassVar, Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_base.adapters.controller.tasks.retry_policy import TerminalWorkError
from knowledge_base.domain.providers.work_queue import UNKNOWN_KIND, FailureInfo

if TYPE_CHECKING:
    from knowledge_base.adapters.controller.tasks.execution import WorkContext
    from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
        ClaimedWorkItem,
    )


class WorkHandler(Protocol):
    """Exécute un ``kind`` de travail (S0 §5).

    ``execute`` produit d'abord ses effets externes idempotents, hors
    transaction, puis appelle ``ctx.finalize(callback)`` : ``callback(session)``
    écrit l'état métier dans la transaction qui passe l'item ``SUCCEEDED``.
    """

    kind: ClassVar[str]
    queue: ClassVar[str]
    max_attempts: ClassVar[int]

    async def execute(self, item: ClaimedWorkItem, ctx: WorkContext) -> None: ...


@runtime_checkable
class HandlesTerminalFailure(Protocol):
    """Hook facultatif d'un ``WorkHandler`` : écrit l'échec métier dans la
    transaction de ``fail`` (rollback si l'item n'est plus la tentative
    courante)."""

    async def on_terminal_failure(
        self, session: AsyncSession, item: ClaimedWorkItem, error: FailureInfo
    ) -> None: ...


class DuplicateHandlerError(ValueError):
    """Deux handlers pour le même ``kind``."""


class HandlerRegistry:
    def __init__(self, handlers: Iterable[WorkHandler] = ()) -> None:
        self._handlers: dict[str, WorkHandler] = {}
        for handler in handlers:
            self.register(handler)

    def register(self, handler: WorkHandler) -> None:
        existing = self._handlers.get(handler.kind)
        if existing is not None:
            raise DuplicateHandlerError(
                f"kind {handler.kind!r} déjà servi par {type(existing).__name__}, "
                f"refusé pour {type(handler).__name__}"
            )
        self._handlers[handler.kind] = handler

    def get(self, kind: str) -> WorkHandler:
        handler = self._handlers.get(kind)
        if handler is None:
            raise TerminalWorkError(
                UNKNOWN_KIND, f"aucun handler enregistré pour le kind {kind!r}"
            )
        return handler

    def max_attempts_by_kind(self) -> dict[str, int]:
        """Nombre de tentatives déclaré par chaque handler, appliqué au claim."""
        return {kind: h.max_attempts for kind, h in self._handlers.items()}

    def kinds_by_queue(self) -> dict[str, frozenset[str]]:
        grouped: defaultdict[str, set[str]] = defaultdict(set)
        for handler in self._handlers.values():
            grouped[handler.queue].add(handler.kind)
        return {queue: frozenset(kinds) for queue, kinds in grouped.items()}
