from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class RetryIngestionResult:
    """L'item rouvert, ``PENDING`` : le dispatch le reprendra."""

    item_id: UUID
    kind: str
