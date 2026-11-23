"""Schémas HTTP de la consultation et du rejeu des échecs (S8), distincts des
vues et résultats des cas d'usage."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class FailureResponse(BaseModel):
    """Un item ``FAILED`` ; ``replayable`` d'après le catalogue des erreurs ;
    ``document_id``, ``version_id``, ``index_id`` : ceux du payload de l'item,
    ``null`` s'il n'en porte pas."""

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


class FailuresResponse(BaseModel):
    items: list[FailureResponse]


class RetryResponse(BaseModel):
    """L'item rouvert : le dispatch le reprendra."""

    item_id: UUID
    kind: str
    status: Literal["PENDING"] = "PENDING"
