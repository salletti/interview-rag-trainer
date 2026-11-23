"""Endpoints de santé.

- ``/health/live`` : le processus répond. Aucune dépendance externe
  interrogée.
- ``/health/ready`` : disponibilité des dépendances. PostgreSQL est
  **requis** (sans lui, aucun cas d'usage ne peut persister : 503). Qdrant et
  RabbitMQ sont sondés et rapportés mais **non bloquants** (``degraded``) :
  sans Qdrant, l'API sert encore l'import de documents (S2), qui n'écrit que
  dans PostgreSQL et le volume des originaux ; seule la recherche (S5) échoue,
  en 500, jamais en faux résultat vide. RabbitMQ n'est pas appelé par l'API :
  les work items passent par la table ``work_items`` (ADR 0004). Retirer
  l'API du trafic quand Qdrant manque couperait l'import sans protéger la
  recherche (docs/decisions/0006-technical-bootstrap.md ; décision de la
  revue S5). Le modèle d'embeddings n'est jamais sondé : il est chargé à la
  première recherche.

Les réponses ne contiennent jamais d'hôte, de port, d'identifiant ni de DSN.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel

from runtime import database, messaging, vector_store
from runtime.settings import Settings

router = APIRouter(prefix="/health", tags=["health"])

ComponentStatus = Literal["ok", "unavailable"]
ReadinessStatus = Literal["ready", "degraded", "not_ready"]


class LiveResponse(BaseModel):
    status: Literal["alive"]


class ComponentCheck(BaseModel):
    status: ComponentStatus
    required: bool


class ReadyResponse(BaseModel):
    status: ReadinessStatus
    checks: dict[str, ComponentCheck]


@router.get("/live", response_model=LiveResponse)
async def live() -> LiveResponse:
    return LiveResponse(status="alive")


async def evaluate_readiness(request: Request) -> ReadyResponse:
    settings: Settings = request.app.state.settings
    timeout = settings.external_timeout_seconds

    postgres_ok, qdrant_ok, rabbitmq_ok = await asyncio.gather(
        database.check_connection(request.app.state.db_engine, timeout),
        vector_store.check_connection(request.app.state.qdrant_client, timeout),
        messaging.check_connection(settings, timeout),
    )

    checks = {
        "postgres": ComponentCheck(
            status="ok" if postgres_ok else "unavailable", required=True
        ),
        "qdrant": ComponentCheck(
            status="ok" if qdrant_ok else "unavailable", required=False
        ),
        "rabbitmq": ComponentCheck(
            status="ok" if rabbitmq_ok else "unavailable", required=False
        ),
    }
    overall: ReadinessStatus
    if any(c.required and c.status != "ok" for c in checks.values()):
        overall = "not_ready"
    elif all(c.status == "ok" for c in checks.values()):
        overall = "ready"
    else:
        overall = "degraded"
    return ReadyResponse(status=overall, checks=checks)


@router.get(
    "/ready",
    response_model=ReadyResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadyResponse}},
)
async def ready(request: Request, response: Response) -> ReadyResponse:
    result = await evaluate_readiness(request)
    if result.status == "not_ready":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return result
