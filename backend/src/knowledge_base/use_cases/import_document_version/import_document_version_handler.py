from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from uuid import UUID, uuid4

from knowledge_base.domain.providers.document_blob_store import DocumentBlobStore
from knowledge_base.domain.providers.unit_of_work import UnitOfWork
from knowledge_base.domain.value_objects.document_source import DocumentSource
from knowledge_base.use_cases.errors import (
    DocumentNotFoundError,
    DuplicateContentOtherDocumentError,
)
from knowledge_base.use_cases.import_document_version.import_document_version_command import (  # noqa: E501
    ImportDocumentVersion,
)
from knowledge_base.use_cases.import_result import (
    ImportDocumentResult,
)
from knowledge_base.use_cases.import_steps import (
    accepted_filename,
    accepted_media_type,
    remember,
    replay,
    request_fingerprint,
    retry_once_after_race,
    schedule_extraction,
    utc_now,
)

logger = logging.getLogger(__name__)


class ImportDocumentVersionHandler:
    """Version suivante d'un document existant ; les versions antérieures non
    publiées sont écartées (I6) et leur extraction annulée."""

    def __init__(
        self,
        *,
        unit_of_work: Callable[[], UnitOfWork],
        blob_store: DocumentBlobStore,
        max_upload_bytes: int,
        id_factory: Callable[[], UUID] = uuid4,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._blob_store = blob_store
        self._max_upload_bytes = max_upload_bytes
        self._id_factory = id_factory
        self._clock = clock

    async def handle(self, command: ImportDocumentVersion) -> ImportDocumentResult:
        filename = accepted_filename(command.filename)
        media_type = accepted_media_type(filename, command.media_type)
        blob = await self._blob_store.put(
            command.content, max_bytes=self._max_upload_bytes
        )
        source = DocumentSource(
            filename=filename,
            media_type=media_type,
            size_bytes=blob.size_bytes,
            content_hash=blob.content_hash,
            imported_at=self._clock(),
        )
        fingerprint = request_fingerprint(
            {
                "operation": "import_document_version",
                "document_id": str(command.document_id),
                "title": None,
                "kind": None,
                "topics": None,
                "language": None,
                "filename": filename,
                "media_type": media_type,
                "content_hash": blob.content_hash.value,
            }
        )
        version_id = self._id_factory()
        return await retry_once_after_race(
            lambda: self._import(command, source, fingerprint, version_id)
        )

    async def _import(
        self,
        command: ImportDocumentVersion,
        source: DocumentSource,
        fingerprint: str,
        version_id: UUID,
    ) -> ImportDocumentResult:
        async with self._unit_of_work() as uow:
            replayed = await replay(uow, command.idempotency_key, fingerprint)
            if replayed is not None:
                return replayed
            document = await uow.documents.get(command.document_id)
            if document is None:
                raise DocumentNotFoundError(command.document_id)
            owner = await uow.documents.find_by_content_hash(source.content_hash)
            if owner is not None and owner.document_id != document.document_id:
                raise DuplicateContentOtherDocumentError(
                    owner.document_id, owner.version_id
                )
            # Même document : I3 est refusé par l'agrégat (DuplicateContentError).
            version = document.import_version(version_id=version_id, source=source)
            await uow.documents.save(document)
            await schedule_extraction(
                uow, document.document_id, version_id, document.pull_events()
            )
            result = ImportDocumentResult(
                document_id=document.document_id,
                version_id=version_id,
                number=version.number,
                created=True,
            )
            await remember(uow, command.idempotency_key, fingerprint, result)
            await uow.commit()
        logger.info(
            "import : document %s, version n°%d %s (%d octets)",
            result.document_id,
            result.number,
            result.version_id,
            source.size_bytes,
        )
        return result
