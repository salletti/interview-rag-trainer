"""Doubles en mémoire des frontières de S2 (Unit of Work, dépôts, work queue,
blob store) et harnais des handlers d'import.

Seules les frontières sont remplacées : toute la logique d'import (empreinte,
dédoublonnage, I3, I6, enqueue, idempotence) reste celle des handlers et de
l'agrégat réels. La Unit of Work travaille sur une copie de l'état et ne le
publie qu'au ``commit`` : un rollback (ou une sortie sans commit) ne laisse
rien, comme PostgreSQL.

API figée par ces tests (ports du domaine) :

- ``knowledge_base.domain.providers.unit_of_work.UnitOfWork`` : gestionnaire de
  contexte asynchrone ; membres en lecture seule ``documents``, ``work_queue``,
  ``idempotency_keys`` ; ``await commit()`` ; sortie du bloc sans ``commit``
  (ou sur exception) = rollback.
- ``...knowledge_document_repository.KnowledgeDocumentRepository`` :
  ``get(document_id) -> KnowledgeDocument | None``,
  ``find_by_content_hash(content_hash) -> KnowledgeDocument | None`` (toute
  version, quel que soit son statut), ``add(document)``, ``save(document)``
  (lève ``ConcurrentModificationError`` si le verrou optimiste est perdu).
- ``...idempotency_key_repository.IdempotencyKeyRepository`` : ``get(key)``,
  ``add(record)`` ; ``IdempotencyRecord(key, fingerprint, document_id,
  version_id, number, created)``.
- ``...document_blob_store.DocumentBlobStore`` :
  ``put(chunks, *, max_bytes) -> StoredBlob`` (lève ``BlobTooLargeError`` sans
  rien laisser), ``exists(content_hash)`` ; ``StoredBlob(content_hash,
  size_bytes)``.
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

from knowledge_base.domain.entities.knowledge_document import KnowledgeDocument
from knowledge_base.domain.providers.document_blob_store import (
    BlobTooLargeError,
    StoredBlob,
)
from knowledge_base.domain.providers.idempotency_key_repository import (
    IdempotencyRecord,
)
from knowledge_base.domain.providers.knowledge_document_repository import (
    ConcurrentModificationError,
)
from knowledge_base.domain.value_objects.content_hash import ContentHash
from knowledge_base.use_cases.import_document.import_document_handler import (
    ImportDocumentHandler,
)
from knowledge_base.use_cases.import_document_version.import_document_version_handler import (  # noqa: E501
    ImportDocumentVersionHandler,
)

NOW = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)
MAX_UPLOAD_BYTES = 1000


@dataclass
class FakeWorkItem:
    kind: str
    dedup_key: str
    payload: dict[str, str]
    status: str = "PENDING"


@dataclass
class State:
    """État « commité » partagé par toutes les Units of Work d'un test."""

    documents: dict[UUID, KnowledgeDocument] = field(default_factory=dict)
    items: dict[str, FakeWorkItem] = field(default_factory=dict)
    keys: dict[str, IdempotencyRecord] = field(default_factory=dict)
    cancel_calls: list[str] = field(default_factory=list)
    # Pannes injectées.
    fail_commit: bool = False
    conflict_on_save: bool = False
    fail_enqueue: bool = False


class FakeDocuments:
    def __init__(self, working: State) -> None:
        self._w = working

    async def get(self, document_id: UUID) -> KnowledgeDocument | None:
        return self._w.documents.get(document_id)

    async def find_by_content_hash(
        self, content_hash: ContentHash
    ) -> KnowledgeDocument | None:
        for document in self._w.documents.values():
            if any(v.source.content_hash == content_hash for v in document.versions):
                return document
        return None

    async def add(self, document: KnowledgeDocument) -> None:
        self._w.documents[document.document_id] = document

    async def save(self, document: KnowledgeDocument) -> None:
        if self._w.conflict_on_save:
            raise ConcurrentModificationError(document.document_id)
        self._w.documents[document.document_id] = document


class FakeWorkQueue:
    def __init__(self, working: State) -> None:
        self._w = working

    async def enqueue(
        self,
        kind: str,
        dedup_key: str,
        payload: Mapping[str, str] | None = None,
        correlation_id: UUID | None = None,
        *,
        max_attempts: int | None = None,
    ) -> UUID:
        if self._w.fail_enqueue:
            raise RuntimeError("work queue indisponible")
        self._w.items.setdefault(
            dedup_key, FakeWorkItem(kind, dedup_key, dict(payload or {}))
        )
        return uuid4()

    async def cancel(self, dedup_key: str) -> bool:
        self._w.cancel_calls.append(dedup_key)
        item = self._w.items.get(dedup_key)
        if item is None or item.status not in {"PENDING", "RUNNING"}:
            return False
        item.status = "CANCELLED"
        return True


class FakeIdempotencyKeys:
    def __init__(self, working: State) -> None:
        self._w = working

    async def get(self, key: str) -> IdempotencyRecord | None:
        return self._w.keys.get(key)

    async def add(self, record: IdempotencyRecord) -> None:
        self._w.keys[record.key] = record


class FakeUnitOfWork:
    def __init__(self, state: State) -> None:
        self._state = state
        self._working = State()
        self.documents = FakeDocuments(self._working)
        self.work_queue = FakeWorkQueue(self._working)
        self.idempotency_keys = FakeIdempotencyKeys(self._working)

    async def __aenter__(self) -> Self:
        snapshot = copy.deepcopy(self._state)
        self._working.documents = snapshot.documents
        self._working.items = snapshot.items
        self._working.keys = snapshot.keys
        self._working.cancel_calls = []
        self._working.fail_enqueue = self._state.fail_enqueue
        self._working.conflict_on_save = self._state.conflict_on_save
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None  # rollback implicite : l'état commité n'a pas été touché

    async def commit(self) -> None:
        if self._state.fail_commit:
            raise RuntimeError("commit refusé")
        self._state.documents = self._working.documents
        self._state.items = self._working.items
        self._state.keys = self._working.keys
        self._state.cancel_calls.extend(self._working.cancel_calls)


class InMemoryBlobStore:
    """Même contrat que l'adapter système de fichiers : rien n'est conservé si
    le flux dépasse ``max_bytes`` ou échoue."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.put_calls = 0

    async def put(self, chunks: AsyncIterable[bytes], *, max_bytes: int) -> StoredBlob:
        self.put_calls += 1
        received = bytearray()
        async for chunk in chunks:
            received.extend(chunk)
            if len(received) > max_bytes:
                raise BlobTooLargeError(max_bytes)
        digest = hashlib.sha256(received).hexdigest()
        self.blobs[digest] = bytes(received)
        return StoredBlob(content_hash=ContentHash(digest), size_bytes=len(received))

    async def exists(self, content_hash: ContentHash) -> bool:
        return content_hash.value in self.blobs


async def stream(data: bytes, chunk_size: int = 7) -> AsyncIterator[bytes]:
    """Contenu fourni par petits morceaux, comme un upload multipart."""
    for start in range(0, len(data), chunk_size):
        yield data[start : start + chunk_size]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class Harness:
    state: State
    blobs: InMemoryBlobStore
    import_document: ImportDocumentHandler
    import_version: ImportDocumentVersionHandler

    def pending_item_for(self, version_id: UUID) -> FakeWorkItem | None:
        return self.state.items.get(f"extract_version:{version_id}")


def make_harness(*, max_upload_bytes: int = MAX_UPLOAD_BYTES) -> Harness:
    state = State()
    blobs = InMemoryBlobStore()

    def unit_of_work() -> FakeUnitOfWork:
        return FakeUnitOfWork(state)

    return Harness(
        state=state,
        blobs=blobs,
        import_document=ImportDocumentHandler(
            unit_of_work=unit_of_work,
            blob_store=blobs,
            max_upload_bytes=max_upload_bytes,
            clock=lambda: NOW,
        ),
        import_version=ImportDocumentVersionHandler(
            unit_of_work=unit_of_work,
            blob_store=blobs,
            max_upload_bytes=max_upload_bytes,
            clock=lambda: NOW,
        ),
    )
