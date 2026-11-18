"""Traduction des refus d'import en réponses HTTP ``{code, message, ...}``."""

from __future__ import annotations

from typing import Any

from fastapi import status
from fastapi.responses import JSONResponse

from knowledge_base.domain.exceptions.document_errors import (
    DocumentWithdrawnError,
    DuplicateContentError,
)
from knowledge_base.domain.providers.document_blob_store import BlobTooLargeError
from knowledge_base.domain.providers.idempotency_key_repository import (
    IdempotencyKeyTakenError,
)
from knowledge_base.domain.providers.knowledge_document_repository import (
    ConcurrentModificationError,
    ContentHashTakenError,
)
from knowledge_base.use_cases.errors import (
    DocumentNotFoundError,
    DuplicateContentOtherDocumentError,
    IdempotencyKeyConflictError,
    InvalidFilenameError,
    UnsupportedDocumentTypeError,
)

IMPORT_REFUSALS = (
    InvalidFilenameError,
    BlobTooLargeError,
    UnsupportedDocumentTypeError,
    DocumentNotFoundError,
    DuplicateContentError,
    DuplicateContentOtherDocumentError,
    IdempotencyKeyConflictError,
    DocumentWithdrawnError,
    ConcurrentModificationError,
    ContentHashTakenError,
    IdempotencyKeyTakenError,
)


def refusal_response(error: Exception) -> JSONResponse:
    status_code, code, extra = _describe(error)
    return JSONResponse(
        status_code=status_code,
        content={"code": code, "message": str(error), **extra},
    )


def _describe(error: Exception) -> tuple[int, str, dict[str, Any]]:
    match error:
        case InvalidFilenameError():
            return (
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "INVALID_FILENAME",
                {"reason": error.reason},
            )
        case BlobTooLargeError():
            return (
                status.HTTP_413_CONTENT_TOO_LARGE,
                "PAYLOAD_TOO_LARGE",
                {"max_bytes": error.max_bytes},
            )
        case UnsupportedDocumentTypeError():
            return (
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                "UNSUPPORTED_MEDIA_TYPE",
                {"filename": error.filename, "media_type": error.media_type},
            )
        case DocumentNotFoundError():
            return (
                status.HTTP_404_NOT_FOUND,
                "DOCUMENT_NOT_FOUND",
                {"document_id": str(error.document_id)},
            )
        case DuplicateContentOtherDocumentError():
            return _duplicate(error.document_id, error.version_id)
        case DuplicateContentError():
            return _duplicate(error.document_id, error.existing_version_id)
        case IdempotencyKeyConflictError():
            return status.HTTP_409_CONFLICT, "IDEMPOTENCY_KEY_CONFLICT", {}
        case DocumentWithdrawnError():
            return (
                status.HTTP_409_CONFLICT,
                "DOCUMENT_WITHDRAWN",
                {"document_id": str(error.document_id)},
            )
        # Une course perdue deux fois de suite (rarissime) : le client rejoue,
        # comme pour un verrou optimiste perdu.
        case (
            ConcurrentModificationError()
            | ContentHashTakenError()
            | IdempotencyKeyTakenError()
        ):
            return status.HTTP_409_CONFLICT, "CONCURRENT_MODIFICATION", {}
    raise TypeError(f"refus d'import non traduit : {type(error).__name__}") from error


def _duplicate(
    document_id: object, version_id: object
) -> tuple[int, str, dict[str, Any]]:
    return (
        status.HTTP_409_CONFLICT,
        "DUPLICATE_CONTENT",
        {"document_id": str(document_id), "version_id": str(version_id)},
    )
