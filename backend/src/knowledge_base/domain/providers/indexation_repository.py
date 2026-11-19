"""Port de persistance de l'agrégat ``Indexation`` (knowledge-base.md §3.3),
identifié par ``(version_id, index_id)``."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from knowledge_base.domain.entities.indexation import Indexation


class IndexationRepository(Protocol):
    async def get(self, version_id: UUID, index_id: UUID) -> Indexation | None: ...

    async def add(self, indexation: Indexation) -> None: ...

    async def save(self, indexation: Indexation) -> None: ...
