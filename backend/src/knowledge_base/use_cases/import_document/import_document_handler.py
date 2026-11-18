from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from uuid import UUID, uuid4

from knowledge_base.domain.entities.knowledge_document import (
    DocumentStatus,
    KnowledgeDocument,
)
from knowledge_base.domain.exceptions.document_errors import DocumentWithdrawnError
from knowledge_base.domain.providers.document_blob_store import DocumentBlobStore
from knowledge_base.domain.providers.knowledge_document_repository import (
    ContentOwner,
)
from knowledge_base.domain.providers.unit_of_work import UnitOfWork
from knowledge_base.domain.value_objects.document_source import DocumentSource
from knowledge_base.use_cases.import_document.import_document_command import (
    ImportDocument,
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


class ImportDocumentHandler:
    """Nouveau document, ou document existant si son contenu est déjà connu
    (dédoublonnage par empreinte, quelles que soient les métadonnées)."""

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

    async def handle(self, command: ImportDocument) -> ImportDocumentResult:
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
                "operation": "import_document",
                "document_id": None,
                "title": command.title,
                "kind": command.kind.value,
                "topics": list(command.topics),
                "language": command.language,
                "filename": filename,
                "media_type": media_type,
                "content_hash": blob.content_hash.value,
            }
        )
        document_id, version_id = self._id_factory(), self._id_factory()
        return await retry_once_after_race(
            lambda: self._import(command, source, fingerprint, document_id, version_id)
        )

    async def _import(
        self,
        command: ImportDocument,
        source: DocumentSource,
        fingerprint: str,
        document_id: UUID,
        version_id: UUID,
    ) -> ImportDocumentResult:
        async with self._unit_of_work() as uow:
            replayed = await replay(uow, command.idempotency_key, fingerprint)
            if replayed is not None:
                return replayed
            existing = await uow.documents.find_by_content_hash(source.content_hash)
            if existing is None:
                document = KnowledgeDocument.import_new(
                    document_id=document_id,
                    version_id=version_id,
                    title=command.title,
                    kind=command.kind,
                    topics=command.topics,
                    language=command.language,
                    source=source,
                )
                await uow.documents.add(document)
                await schedule_extraction(
                    uow, document_id, version_id, document.pull_events()
                )
                result = ImportDocumentResult(
                    document_id=document_id,
                    version_id=version_id,
                    number=document.get_version(version_id).number,
                    created=True,
                )
            else:
                result = _known_content(existing)
            await remember(uow, command.idempotency_key, fingerprint, result)
            await uow.commit()
        logger.info(
            "import %s : document %s, version %s (%d octets)",
            "créé" if result.created else "déjà connu",
            result.document_id,
            result.version_id,
            source.size_bytes,
        )
        return result


def _known_content(owner: ContentOwner) -> ImportDocumentResult:
    """Le contenu d'un document retiré ne peut pas être réimporté : le rendre
    tel quel ferait croire à un import réussi d'un document invisible (O11)."""
    if owner.document_status is DocumentStatus.WITHDRAWN:
        raise DocumentWithdrawnError(owner.document_id, "import de son contenu")
    return ImportDocumentResult(
        document_id=owner.document_id,
        version_id=owner.version_id,
        number=owner.version_number,
        created=False,
    )
