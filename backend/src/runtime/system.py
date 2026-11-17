"""Informations système non sensibles : ``GET /api/v1/system/info``.

Sert au frontend et au diagnostic. N'expose ni hôte, ni port, ni identifiant,
ni DSN : uniquement des noms et versions.
"""

from __future__ import annotations

import platform

from fastapi import APIRouter, Request
from pydantic import BaseModel

from runtime.settings import Environment, Settings

router = APIRouter(prefix="/api/v1/system", tags=["system"])

# Bounded Contexts validés (ADR 0001). Liste descriptive tant qu'aucun
# contexte n'enregistre de code dans le composition root.
BOUNDED_CONTEXTS: tuple[str, ...] = ("interview_training", "assessment")


class SystemInfo(BaseModel):
    name: str
    version: str
    environment: Environment
    python_version: str
    bounded_contexts: list[str]
    docs_url: str | None


@router.get("/info", response_model=SystemInfo)
async def info(request: Request) -> SystemInfo:
    settings: Settings = request.app.state.settings
    return SystemInfo(
        name=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
        python_version=platform.python_version(),
        bounded_contexts=list(BOUNDED_CONTEXTS),
        docs_url=request.app.docs_url,
    )
