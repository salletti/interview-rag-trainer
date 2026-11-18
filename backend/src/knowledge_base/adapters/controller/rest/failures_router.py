"""Routes de consultation et de rejeu des échecs de traitement (S8 ; ADR 0004
points 10 et 11 : pas de DLQ RabbitMQ en V1, l'état ``FAILED`` en base joue le
rôle de dead-letter, consultable et relançable ; O10).

Le routeur traduit HTTP en commande et le résultat (ou le refus) en réponse ;
il ne décide rien.

Pas de ``from __future__ import annotations`` ici : FastAPI doit résoudre les
annotations des routes, qui référencent des dépendances définies localement.
"""

from collections.abc import Callable
from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse

from knowledge_base.adapters.controller.rest.documents_router import PREFIX, TAGS
from knowledge_base.adapters.controller.rest.errors import (
    RETRY_REFUSALS,
    refusal_response,
)
from knowledge_base.adapters.controller.rest.failures_schemas import (
    FailureResponse,
    FailuresResponse,
    RetryResponse,
)
from knowledge_base.adapters.controller.rest.schemas import ErrorResponse
from knowledge_base.use_cases.list_failures.failure_view import FailureView
from knowledge_base.use_cases.list_failures.list_failures_command import (
    MAX_FAILURES_LIMIT,
    ListFailures,
)
from knowledge_base.use_cases.list_failures.list_failures_handler import (
    ListFailuresHandler,
)
from knowledge_base.use_cases.retry_ingestion.retry_ingestion_command import (
    RetryIngestion,
)
from knowledge_base.use_cases.retry_ingestion.retry_ingestion_handler import (
    RetryIngestionHandler,
)

ListFailuresHandlerFactory = Callable[[], ListFailuresHandler]
RetryIngestionHandlerFactory = Callable[[], RetryIngestionHandler]

_RETRY_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": "Item inconnu (FAILURE_NOT_FOUND)",
    },
    status.HTTP_409_CONFLICT: {
        "model": ErrorResponse,
        "description": "Item pas FAILED (NOT_FAILED), échec non rejouable ou "
        "cycle de réconciliation (NOT_REPLAYABLE), travail sans objet "
        "(OBSOLETE_WORK) ou document modifié pendant le rejeu "
        "(CONCURRENT_MODIFICATION)",
    },
}


def add_failure_routes(
    router: APIRouter,
    *,
    list_failures_handler: ListFailuresHandlerFactory,
    retry_ingestion_handler: RetryIngestionHandlerFactory,
) -> None:
    """Les fabriques sont appelées à chaque requête (dépendances FastAPI)."""

    @router.get(
        f"{PREFIX}/failures",
        tags=TAGS,
        response_model=FailuresResponse,
        summary="Consulter les traitements en échec",
    )
    async def list_failures(
        handler: Annotated[ListFailuresHandler, Depends(list_failures_handler)],
        limit: Annotated[int, Query(ge=1, le=MAX_FAILURES_LIMIT)] = 100,
    ) -> FailuresResponse:
        """Items ``FAILED``, du plus récent au plus ancien, hors cycles de
        réconciliation (le cycle suivant les remplace)."""
        views = await handler.handle(ListFailures(limit=limit))
        return FailuresResponse(items=[_failure(view) for view in views])

    @router.post(
        f"{PREFIX}/failures/{{item_id}}/retry",
        tags=TAGS,
        status_code=status.HTTP_202_ACCEPTED,
        response_model=RetryResponse,
        responses=_RETRY_RESPONSES,
        summary="Rejouer un traitement en échec",
    )
    async def retry_failure(
        item_id: UUID,
        handler: Annotated[RetryIngestionHandler, Depends(retry_ingestion_handler)],
    ) -> RetryResponse | JSONResponse:
        """Rouvre l'item ``FAILED`` et remet en attente la version ou
        l'indexation qu'il désigne, dans une transaction."""
        try:
            result = await handler.handle(RetryIngestion(item_id=item_id))
        except RETRY_REFUSALS as refused:
            return refusal_response(refused)
        return RetryResponse(item_id=result.item_id, kind=result.kind)


def _failure(view: FailureView) -> FailureResponse:
    return FailureResponse(
        item_id=view.item_id,
        kind=view.kind,
        code=view.code,
        message=view.message,
        attempts=view.attempts,
        max_attempts=view.max_attempts,
        failed_at=view.failed_at,
        replayable=view.replayable,
        document_id=view.document_id,
        version_id=view.version_id,
        index_id=view.index_id,
    )
