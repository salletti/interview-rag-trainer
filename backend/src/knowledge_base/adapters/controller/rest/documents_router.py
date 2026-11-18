"""Routes d'import de Knowledge Base (S2). Le routeur traduit HTTP en commande
et le résultat (ou le refus) en réponse ; il ne décide rien.

Le fichier est transmis au cas d'usage par morceaux : Starlette a déjà
stocké la partie multipart dans un fichier temporaire (en mémoire jusqu'à
1 Mio, sur disque au-delà) ; il n'est jamais lu en entier en mémoire et la
limite de taille s'applique pendant la copie vers le volume des blobs.

Pas de ``from __future__ import annotations`` ici : FastAPI doit résoudre les
annotations des routes, qui référencent des dépendances définies localement.
"""

from collections.abc import AsyncIterator, Callable
from enum import Enum
from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Header, Response, UploadFile, status
from fastapi.responses import JSONResponse

from knowledge_base.adapters.controller.rest.errors import (
    IMPORT_REFUSALS,
    refusal_response,
)
from knowledge_base.adapters.controller.rest.schemas import (
    ErrorResponse,
    ImportResponse,
)
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

PREFIX: Final = "/api/v1/knowledge-base"
TAGS: Final[list[str | Enum]] = ["knowledge-base"]
UPLOAD_CHUNK_BYTES: Final = 64 * 1024

ImportDocumentHandlerFactory = Callable[[], ImportDocumentHandler]
ImportVersionHandlerFactory = Callable[[], ImportDocumentVersionHandler]
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
) -> None:
    """Déclare les routes directement sur ``router`` (le routeur de
    l'application) : FastAPI ≥ 0.163 n'aplatit plus les routeurs inclus.
    Les fabriques sont appelées à chaque requête (dépendances FastAPI) :
    l'assemblage des handlers appartient à l'appelant, qui peut les
    substituer."""

    @router.post(
        f"{PREFIX}/documents",
        tags=TAGS,
        status_code=status.HTTP_202_ACCEPTED,
        response_model=ImportResponse,
        responses=_RESPONSES,
        summary="Importer un document Markdown ou texte",
    )
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

    @router.post(
        f"{PREFIX}/documents/{{document_id}}/versions",
        tags=TAGS,
        status_code=status.HTTP_202_ACCEPTED,
        response_model=ImportResponse,
        responses=_RESPONSES,
        summary="Importer une nouvelle version d'un document",
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
