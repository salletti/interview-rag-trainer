"""Port des items ``FAILED`` de la work queue (S8 ; ADR 0004 points 10 et 11 :
l'état ``FAILED`` en base joue le rôle de dead-letter, « consultable et
relançable » ; S0 §3, transition ``reopen``).

Consultation (``ListFailures``), réouverture explicite (``RetryIngestion``)
et réouverture bornée d'une purge par la réconciliation. La réouverture
s'exécute dans la transaction de l'appelant, avec la remise en attente de
l'état métier (version ``FAILED`` -> ``RECEIVED``, indexation ``FAILED`` ->
``PENDING``) : les deux sont visibles ensemble ou pas du tout.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from knowledge_base.domain.providers.indexation_repository import (
    IndexationRepository,
)
from knowledge_base.domain.providers.knowledge_corpus_repository import (
    KnowledgeCorpusRepository,
)
from knowledge_base.domain.providers.knowledge_document_repository import (
    KnowledgeDocumentRepository,
)


@dataclass(frozen=True)
class WorkItemRecord:
    """Un item tel qu'il est en base. ``failed_at`` : date d'entrée dans son
    état terminal (``completed_at``), nulle tant qu'il ne l'est pas."""

    item_id: UUID
    kind: str
    status: str
    payload: Mapping[str, str]
    attempts: int
    max_attempts: int
    last_error_code: str | None
    last_error_message: str | None
    failed_at: datetime | None
    reopen_count: int

    def payload_id(self, name: str) -> UUID | None:
        """Identifiant ``name`` du payload, ``None`` s'il est absent ou
        illisible (le payload n'est jamais une source d'exception ici)."""
        value = self.payload.get(name)
        if value is None:
            return None
        try:
            return UUID(str(value))
        except ValueError:
            return None


class FailedWorkRepository(Protocol):
    async def get_for_update(self, item_id: UUID) -> WorkItemRecord | None:
        """L'item, verrouillé jusqu'à la fin de la transaction : deux
        réouvertures simultanées se suivent, la seconde voit la première."""
        ...

    async def reopen(self, item_id: UUID) -> bool:
        """``FAILED`` -> ``PENDING`` : tentatives remises à zéro, disponible
        tout de suite, ``dispatched_at`` et ``completed_at`` effacés, dernière
        erreur conservée, jeton de fencing inchangé (jamais reculé),
        ``reopen_count`` incrémenté. Faux si l'item n'est pas ``FAILED``."""
        ...

    async def list_failed(self, limit: int) -> Sequence[WorkItemRecord]:
        """Items ``FAILED``, du plus récent au plus ancien (``failed_at``)."""
        ...


class FailedWorkUnitOfWork(Protocol):
    """``async with`` ouvre la transaction ; seul ``commit()`` la valide."""

    @property
    def failed_work(self) -> FailedWorkRepository: ...

    @property
    def documents(self) -> KnowledgeDocumentRepository: ...

    @property
    def corpus(self) -> KnowledgeCorpusRepository: ...

    @property
    def indexations(self) -> IndexationRepository: ...

    async def commit(self) -> None: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...
