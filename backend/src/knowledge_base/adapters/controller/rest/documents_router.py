"""Routes d'import (S2) et de retrait (S7) des documents de Knowledge Base. Le
routeur traduit HTTP en commande et le résultat (ou le refus) en réponse ; il
ne décide rien.

Le fichier est transmis au cas d'usage par morceaux : Starlette a déjà
stocké la partie multipart dans un fichier temporaire (en mémoire jusqu'à
1 Mio, sur disque au-delà) ; il n'est jamais lu en entier en mémoire et la
limite de taille s'applique pendant la copie vers le volume des blobs.

Avant cela, un import dont le ``Content-Length`` déclare plus que la limite
(marge du multipart comprise) est refusé sans que son corps soit lu (revue
S2, M-1) : FastAPI lit le formulaire avant toute dépendance, le contrôle est
donc porté par la classe de route. Un en-tête absent ou mensonger laisse le
contrôle au fil du flux, qui reste la seule borne exacte.

Pas de ``from __future__ import annotations`` ici : FastAPI doit résoudre les
annotations des routes, qui référencent des dépendances définies localement.
"""

from collections.abc import AsyncIterator, Callable, Coroutine
from enum import Enum
from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    Form,
    Header,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from knowledge_base.adapters.controller.rest.errors import (
    IMPORT_REFUSALS,
    WITHDRAW_REFUSALS,
    refusal_response,
)
from knowledge_base.adapters.controller.rest.schemas import (
    ErrorResponse,
    ImportResponse,
    WithdrawResponse,
)
from knowledge_base.domain.providers.document_blob_store import BlobTooLargeError
from knowledge_base.domain.value_objects.document_kind import DocumentKind
from knowledge_base.use_cases.import_document.import_document_command import (
    ImportDocument,
)
from knowledge_base.use_cases.import_document.import_document_handler import (
    ImportDocumentHandler,
)
from knowledge_base.use_cases.import_document_version.import_document_version_command import (  # noqa: E501
    ImportDocumentVersion,
)
from knowledge_base.use_cases.import_document_version.import_document_version_handler import (  # noqa: E501
    ImportDocumentVersionHandler,
)
from knowledge_base.use_cases.import_result import (
    ImportDocumentResult,
)
from knowledge_base.use_cases.withdraw_document.withdraw_document_command import (
    WithdrawDocument,
)
from knowledge_base.use_cases.withdraw_document.withdraw_document_handler import (
    WithdrawDocumentHandler,
)

PREFIX: Final = "/api/v1/knowledge-base"
TAGS: Final[list[str | Enum]] = ["knowledge-base"]
UPLOAD_CHUNK_BYTES: Final = 64 * 1024
# Le Content-Length compte tout le corps multipart (frontières, en-têtes des
# parties, titre, sujets…), pas le seul fichier : sans marge, un fichier à la
# limite serait refusé d'avance. La borne exacte reste celle du flux.
MULTIPART_ALLOWANCE_BYTES: Final = 64 * 1024

ImportDocumentHandlerFactory = Callable[[], ImportDocumentHandler]
ImportVersionHandlerFactory = Callable[[], ImportDocumentVersionHandler]
WithdrawDocumentHandlerFactory = Callable[[], WithdrawDocumentHandler]
MaxUploadBytes = Callable[[], int]
RouteHandler = Callable[[Request], Coroutine[Any, Any, Response]]
IdempotencyKey = Annotated[str | None, Header(min_length=1, max_length=255)]

_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    status.HTTP_200_OK: {
        "model": ImportResponse,
        "description": "Contenu déjà connu : version existante",
    },
    status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
    status.HTTP_409_CONFLICT: {"model": ErrorResponse},
    status.HTTP_413_CONTENT_TOO_LARGE: {"model": ErrorResponse},
    status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: {"model": ErrorResponse},
}


def add_document_routes(
    router: APIRouter,
    *,
    import_document_handler: ImportDocumentHandlerFactory,
    import_version_handler: ImportVersionHandlerFactory,
    withdraw_document_handler: WithdrawDocumentHandlerFactory,
    max_upload_bytes: MaxUploadBytes,
) -> None:
    """Déclare les routes directement sur ``router`` (le routeur de
    l'application) : FastAPI ≥ 0.163 n'aplatit plus les routeurs inclus.
    Les fabriques sont appelées à chaque requête (dépendances FastAPI) :
    l'assemblage des handlers appartient à l'appelant, qui peut les
    substituer. ``max_upload_bytes`` (appelé à chaque import) borne le
    ``Content-Length`` déclaré des routes d'import."""
    import_route = _declared_length_limited_route(max_upload_bytes)

    async def import_document(
        response: Response,
        handler: Annotated[ImportDocumentHandler, Depends(import_document_handler)],
        file: UploadFile,
        # pattern : au moins un caractère non blanc (recherche non ancrée).
        title: Annotated[str, Form(min_length=1, pattern=r"\S")],
        kind: Annotated[DocumentKind, Form()],
        language: Annotated[str, Form(min_length=1, max_length=35)],
        topics: Annotated[list[str] | None, Form()] = None,
        idempotency_key: IdempotencyKey = None,
    ) -> ImportResponse | JSONResponse:
        command = ImportDocument(
            title=title,
            kind=kind,
            topics=tuple(topics or ()),
            language=language,
            filename=file.filename or "",
            media_type=file.content_type or "",
            content=_chunks(file),
            idempotency_key=idempotency_key,
        )
        try:
            result = await handler.handle(command)
        except IMPORT_REFUSALS as refused:
            return refusal_response(refused)
        return _accepted(result, response)

    router.add_api_route(
        f"{PREFIX}/documents",
        import_document,
        methods=["POST"],
        tags=TAGS,
        status_code=status.HTTP_202_ACCEPTED,
        response_model=ImportResponse,
        responses=_RESPONSES,
        summary="Importer un document Markdown ou texte",
        route_class_override=import_route,
    )

    async def import_document_version(
        document_id: UUID,
        response: Response,
        handler: Annotated[
            ImportDocumentVersionHandler, Depends(import_version_handler)
        ],
        file: UploadFile,
        idempotency_key: IdempotencyKey = None,
    ) -> ImportResponse | JSONResponse:
        command = ImportDocumentVersion(
            document_id=document_id,
            filename=file.filename or "",
            media_type=file.content_type or "",
            content=_chunks(file),
            idempotency_key=idempotency_key,
        )
        try:
            result = await handler.handle(command)
        except IMPORT_REFUSALS as refused:
            return refusal_response(refused)
        return _accepted(result, response)

    router.add_api_route(
        f"{PREFIX}/documents/{{document_id}}/versions",
        import_document_version,
        methods=["POST"],
        tags=TAGS,
        status_code=status.HTTP_202_ACCEPTED,
        response_model=ImportResponse,
        responses=_RESPONSES,
        summary="Importer une nouvelle version d'un document",
        route_class_override=import_route,
    )

    @router.delete(
        f"{PREFIX}/documents/{{document_id}}",
        tags=TAGS,
        status_code=status.HTTP_202_ACCEPTED,
        response_model=WithdrawResponse,
        responses={
            status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
            status.HTTP_409_CONFLICT: {"model": ErrorResponse},
        },
        summary="Retirer un document (purge asynchrone)",
    )
    async def withdraw_document(
        document_id: UUID,
        handler: Annotated[WithdrawDocumentHandler, Depends(withdraw_document_handler)],
    ) -> WithdrawResponse | JSONResponse:
        """Retrait logique, immédiat pour toute recherche commencée après la
        réponse ; le document reste comme tombstone, ses données sont purgées
        de façon asynchrone. 409 ``DOCUMENT_WITHDRAWN`` s'il l'est déjà."""
        try:
            result = await handler.handle(WithdrawDocument(document_id=document_id))
        except WITHDRAW_REFUSALS as refused:
            return refusal_response(refused)
        return WithdrawResponse(
            document_id=result.document_id,
            visibility_revision=result.visibility_revision,
        )


def _declared_length_limited_route(max_upload_bytes: MaxUploadBytes) -> type[APIRoute]:
    """Classe de route qui refuse en 413 ``PAYLOAD_TOO_LARGE``, avant toute
    lecture du corps, un ``Content-Length`` au-delà de la limite et de la
    marge du multipart."""

    class DeclaredLengthLimitedRoute(APIRoute):
        def get_route_handler(self) -> RouteHandler:
            handle = super().get_route_handler()

            async def refuse_declared_excess(request: Request) -> Response:
                limit = max_upload_bytes()
                declared = _declared_length(request)
                ceiling = limit + MULTIPART_ALLOWANCE_BYTES
                if declared is not None and declared > ceiling:
                    return refusal_response(BlobTooLargeError(limit))
                return await handle(request)

            return refuse_declared_excess

    return DeclaredLengthLimitedRoute


def _declared_length(request: Request) -> int | None:
    """``None`` si l'en-tête est absent ou illisible : le flux tranchera."""
    value = request.headers.get("content-length", "")
    return int(value) if value.isdecimal() else None


async def _chunks(upload: UploadFile) -> AsyncIterator[bytes]:
    while chunk := await upload.read(UPLOAD_CHUNK_BYTES):
        yield chunk


def _accepted(result: ImportDocumentResult, response: Response) -> ImportResponse:
    """202 si une version a été créée, 200 si le contenu était déjà connu."""
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return ImportResponse(
        document_id=result.document_id,
        version_id=result.version_id,
        number=result.number,
    )
