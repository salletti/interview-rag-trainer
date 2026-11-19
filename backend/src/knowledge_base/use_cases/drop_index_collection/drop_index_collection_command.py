from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class DropIndexCollection:
    """Payload de l'item ``drop_index_collection``."""

    index_id: UUID
