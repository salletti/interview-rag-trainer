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
  ``find_by_content_hash(content_hash) -> ContentOwner | None`` (modèle de
  lecture : document, son statut, version et numéro ; toute version, quel que
  soit son statut), ``add(document)``, ``save(document)``
  (lève ``ConcurrentModificationError`` si le verrou optimiste est perdu).
- ``...idempotency_key_repository.IdempotencyKeyRepository`` : ``get(key)``,
  ``add(record)`` ; ``IdempotencyRecord(key, fingerprint, document_id,
  version_id, number, created)``.
- ``...document_blob_store.DocumentBlobStore`` :
  ``put(chunks, *, max_bytes) -> StoredBlob`` (lève ``BlobTooLargeError`` sans
  rien laisser), ``exists(content_hash)`` ; ``StoredBlob(content_hash,
  size_bytes)``.

Depuis S7, la Unit of Work expose aussi ``corpus`` et ``indexations`` (l'import
d'une version et le retrait annulent l'indexation en cours) : les tests de S2
n'ont pas de corpus (``NoCorpus``), donc aucune indexation à annuler ; les
doubles complets sont ceux de ``indexing_fakes.py``.
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

from knowledge_base.domain.entities.indexation import Indexation
from knowledge_base.domain.entities.knowledge_corpus import KnowledgeCorpus
from knowledge_base.domain.entities.knowledge_document import KnowledgeDocument
from knowledge_base.domain.providers.document_blob_store import (
    BlobTooLargeError,
    StoredBlob,
)
from knowledge_base.domain.providers.idempotency_key_repository import (
    IdempotencyRecord,
)
from knowledge_base.domain.providers.indexation_repository import (
    IndexationRepository,
)
from knowledge_base.domain.providers.knowledge_corpus_repository import (
    KnowledgeCorpusRepository,
)
from knowledge_base.domain.providers.knowledge_document_repository import (
    ConcurrentModificationError,
    ContentOwner,
)
from knowledge_base.domain.value_objects.content_hash import ContentHash
from knowledge_base.use_cases.import_document.import_document_handler import (
    ImportDocumentHandler,
)
from knowledge_base.use_cases.import_document_version.import_document_version_handler import (  # noqa: E501
    ImportDocumentVersionHandler,
)
from knowledge_base.use_cases.withdraw_document.withdraw_document_handler import (
    WithdrawDocumentHandler,
)

NOW = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)
MAX_UPLOAD_BYTES = 1000


@dataclass
class FakeWorkItem:
    kind: str
    dedup_key: str
    payload: dict[str, str]
    status: str = "PENDING"
    # Délai de disponibilité demandé à l'enqueue (S9), ``None`` : immédiat.
    delay: timedelta | None = None


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
    ) -> ContentOwner | None:
        for document in self._w.documents.values():
            for version in document.versions:
                if version.source.content_hash == content_hash:
                    return ContentOwner(
                        document_id=document.document_id,
                        document_status=document.status,
                        version_id=version.version_id,
                        version_number=version.number,
                    )
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
        delay: timedelta | None = None,
    ) -> UUID:
        if self._w.fail_enqueue:
            raise RuntimeError("work queue indisponible")
        self._w.items.setdefault(
            dedup_key, FakeWorkItem(kind, dedup_key, dict(payload or {}), delay=delay)
        )
        return uuid4()

    async def enqueue_again(
        self,
        kind: str,
        dedup_key: str,
        payload: Mapping[str, str] | None = None,
        correlation_id: UUID | None = None,
        *,
        reuse_running: bool = True,
    ) -> UUID:
        family = self._family(dedup_key)
        reusable = {"PENDING", "RUNNING"} if reuse_running else {"PENDING"}
        if not any(item.status in reusable for item in family):
            key = f"{dedup_key}:{len(family) + 1}" if family else dedup_key
            await self.enqueue(kind, key, payload, correlation_id)
        return uuid4()

    async def cancel(self, dedup_key: str) -> bool:
        self._w.cancel_calls.append(dedup_key)
        cancelled = False
        for item in self._family(dedup_key):
            if item.status in {"PENDING", "RUNNING"}:
                item.status = "CANCELLED"
                cancelled = True
        return cancelled

    def _family(self, dedup_key: str) -> list[FakeWorkItem]:
        """La clé et ses générations ``<clé>:<n>`` (port ``WorkQueue``, S8)."""
        return [
            item
            for key, item in self._w.items.items()
            if key == dedup_key or key.startswith(f"{dedup_key}:")
        ]


class FakeIdempotencyKeys:
    def __init__(self, working: State) -> None:
        self._w = working

    async def get(self, key: str) -> IdempotencyRecord | None:
        return self._w.keys.get(key)

    async def add(self, record: IdempotencyRecord) -> None:
        self._w.keys[record.key] = record


class NoCorpus:
    """Corpus jamais créé : la situation des tests de S2 et S3."""

    async def get_shared(self) -> KnowledgeCorpus | None:
        return None

    async def get_for_update(self) -> KnowledgeCorpus | None:
        return None

    async def add(self, corpus: KnowledgeCorpus) -> None:
        raise AssertionError("les tests de S2 et S3 ne créent pas de corpus")

    async def save(self, corpus: KnowledgeCorpus) -> None:
        raise AssertionError("les tests de S2 et S3 ne modifient pas de corpus")


class NoIndexations:
    """Sans corpus, aucune indexation n'est lue ni écrite."""

    async def get(self, version_id: UUID, index_id: UUID) -> Indexation | None:
        return None

    async def add(self, indexation: Indexation) -> None:
        raise AssertionError("aucune indexation attendue sans corpus")

    async def save(self, indexation: Indexation) -> None:
        raise AssertionError("aucune indexation attendue sans corpus")


class FakeUnitOfWork:
    def __init__(self, state: State) -> None:
        self._state = state
        self._working = State()
        self.documents = FakeDocuments(self._working)
        self.work_queue = FakeWorkQueue(self._working)
        self.idempotency_keys = FakeIdempotencyKeys(self._working)
        self.corpus: KnowledgeCorpusRepository = NoCorpus()
        self.indexations: IndexationRepository = NoIndexations()

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
    withdraw_document: WithdrawDocumentHandler

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
        withdraw_document=WithdrawDocumentHandler(unit_of_work=unit_of_work),
    )
