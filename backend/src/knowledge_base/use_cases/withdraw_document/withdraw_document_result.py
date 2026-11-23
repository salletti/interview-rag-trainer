from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class WithdrawDocumentResult:
    """``visibility_revision`` : la révision créée par le retrait (§3.1)."""

    document_id: UUID
    visibility_revision: int
