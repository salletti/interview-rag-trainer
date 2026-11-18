"""Refus de l'agrégat ``Indexation`` (knowledge-base.md §3.3, §5.3)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from knowledge_base.domain.entities.indexation import IndexationStatus


class InvalidIndexationTransitionError(Exception):
    """Transition absente du diagramme de §5.3 ; l'indexation est inchangée."""

    def __init__(
        self,
        version_id: UUID,
        index_id: UUID,
        current: IndexationStatus,
        operation: str,
    ) -> None:
        super().__init__(
            f"indexation ({version_id}, {index_id}) : {operation} interdit "
            f"depuis {current}"
        )
        self.version_id = version_id
        self.index_id = index_id
        self.current = current
        self.operation = operation
