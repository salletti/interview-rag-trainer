from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class AbandonIndexMigration:
    """Index ``BUILDING`` dont la migration est abandonnée (knowledge-base.md
    §5.4, §13)."""

    index_id: UUID
