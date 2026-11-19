"""Schémas HTTP de l'import et du retrait, distincts des commandes et
résultats."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ImportResponse(BaseModel):
    document_id: UUID
    version_id: UUID
    number: int


class WithdrawResponse(BaseModel):
    """Retrait commité (effet immédiat sur la recherche) ; purge planifiée."""

    document_id: UUID
    status: Literal["WITHDRAWN"] = "WITHDRAWN"
    visibility_revision: int


class ErrorResponse(BaseModel):
    """``code`` stable, ``message`` lisible ; champs complémentaires selon le
    code (``document_id``, ``version_id``, ``max_bytes``…)."""

    model_config = ConfigDict(extra="allow")

    code: str
    message: str
