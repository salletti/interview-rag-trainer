from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True)
class FailureView:
    """Un item ``FAILED`` tel que l'exploitation le consulte (ADR 0004 point
    11). ``replayable`` : d'après le catalogue des erreurs, faux pour un code
    qu'il ne connaît pas. ``document_id``, ``version_id``, ``index_id`` : ceux
    du payload de l'item, ``None`` s'il n'en porte pas."""

    item_id: UUID
    kind: str
    code: str | None
    message: str | None
    attempts: int
    max_attempts: int
    failed_at: datetime | None
    replayable: bool
    document_id: UUID | None
    version_id: UUID | None
    index_id: UUID | None
