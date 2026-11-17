"""Schémas HTTP de l'import, distincts des commandes et résultats."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ImportResponse(BaseModel):
    document_id: UUID
    version_id: UUID
    number: int


class ErrorResponse(BaseModel):
    """``code`` stable, ``message`` lisible ; champs complémentaires selon le
    code (``document_id``, ``version_id``, ``max_bytes``…)."""

    model_config = ConfigDict(extra="allow")

    code: str
    message: str
