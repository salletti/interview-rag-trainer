"""Doubles et harnais de S3 (extraction), construits sur ceux de S2.

Le document et sa version ``RECEIVED`` sont créés par les VRAIS handlers
d'import de S2 sur les fakes de ``fakes.py`` ; seules les frontières de S3
sont ajoutées : lecture du blob, dépôt des unités/chunks, Unit of Work qui
expose ``extractions`` et un parseur de test (``MarkdownLiteParser``).

API figée par ces tests (ports du domaine) :

- ``knowledge_base.domain.providers.document_blob_reader.DocumentBlobReader`` :
  ``await read(content_hash: ContentHash) -> bytes``.
- ``knowledge_base.domain.providers.extraction_repository.ExtractionRepository``
  : ``await add_batch(version_id, policy_version, units, chunks)`` ; le lot est
  identifié par ``(version_id, policy_version)``. ``UnitOfWork.extractions`` y
  donne accès, dans la même transaction que ``documents``.
- ``ExtractionTransaction`` (``use_cases/record_extraction``) : ce que
  ``apply`` et ``fail`` reçoivent, un objet exposant ``documents`` et
  ``extractions`` (une ``UnitOfWork`` convient).

Depuis S4, ``apply`` lit aussi le corpus et crée les indexations de la version
(``corpus``, ``indexations``, ``work_queue``). Les tests de S3 n'ont pas de
corpus : ``NoCorpus`` (``fakes.py``, depuis S7) n'en rend aucun, donc aucune
indexation n'est créée ; les doubles complets sont ceux de
``indexing_fakes.py``.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Self
from uuid import UUID

from knowledge_base.domain.entities.knowledge_document import KnowledgeDocument
from knowledge_base.domain.value_objects.content_hash import ContentHash
from knowledge_base.domain.value_objects.document_chunk import DocumentChunk
from knowledge_base.domain.value_objects.document_kind import DocumentKind
from knowledge_base.domain.value_objects.knowledge_unit import KnowledgeUnit
from knowledge_base.domain.value_objects.structured_document import StructuredDocument
from knowledge_base.use_cases.import_document.import_document_command import (
    ImportDocument,
)
from knowledge_base.use_cases.record_extraction.record_extraction_command import (
    RecordExtraction,
)
from knowledge_base.use_cases.record_extraction.record_extraction_handler import (
    RecordExtractionHandler,
)
from tests.unit.knowledge_base.domain.services.support import COUNTER, policy
from tests.unit.knowledge_base.markdown_lite import MarkdownLiteParser
from tests.unit.knowledge_base.use_cases.fakes import (
    FakeDocuments,
    FakeUnitOfWork,
    Harness,
    State,
    make_harness,
    stream,
)


@dataclass(frozen=True)
class StoredBatch:
    version_id: UUID
    policy_version: str
    units: tuple[KnowledgeUnit, ...]
    chunks: tuple[DocumentChunk, ...]


@dataclass
class ExtractionStore:
    """Lots « commités » ; ``commits`` compte les ``commit`` de toute Unit of Work."""

    batches: list[StoredBatch] = field(default_factory=list)
    commits: int = 0
    opened: int = 0


class FakeExtractions:
    def __init__(self, pending: list[StoredBatch]) -> None:
        self._pending = pending

    async def add_batch(
        self,
        version_id: UUID,
        policy_version: str,
        units: Sequence[KnowledgeUnit],
        chunks: Sequence[DocumentChunk],
    ) -> None:
        self._pending.append(
            StoredBatch(version_id, policy_version, tuple(units), tuple(chunks))
        )


class CopyingDocuments(FakeDocuments):
    """Comme une base : ``get`` rend une copie, donc une modification de
    l'agrégat n'existe que si ``save`` est appelé."""

    async def get(self, document_id: UUID) -> KnowledgeDocument | None:
        found = await super().get(document_id)
        return copy.deepcopy(found)


class ExtractionUnitOfWork(FakeUnitOfWork):
    """Unit of Work de S2 + ``extractions`` ; les lots ne sont publiés qu'au
    ``commit`` (comme les documents), jamais sur rollback."""

    def __init__(self, state: State, store: ExtractionStore) -> None:
        super().__init__(state)
        self._store = store
        self._pending: list[StoredBatch] = []
        self.documents = CopyingDocuments(self._working)
        self.extractions = FakeExtractions(self._pending)

    async def __aenter__(self) -> Self:
        self._store.opened += 1
        self._pending.clear()
        return await super().__aenter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._pending.clear()
        return await super().__aexit__(exc_type, exc, tb)

    async def commit(self) -> None:
        await super().commit()
        self._store.commits += 1
        self._store.batches.extend(self._pending)
        self._pending.clear()


class FakeBlobReader:
    """Lit les blobs déposés par le ``InMemoryBlobStore`` de S2."""

    def __init__(self, harness: Harness) -> None:
        self._harness = harness
        self.reads: list[str] = []

    async def read(self, content_hash: ContentHash) -> bytes:
        self.reads.append(content_hash.value)
        return self._harness.blobs.blobs[content_hash.value]


class CountingParser:
    def __init__(self) -> None:
        self._inner = MarkdownLiteParser()
        self.calls = 0

    def parse(
        self, content: bytes, media_type: str, filename: str
    ) -> StructuredDocument:
        self.calls += 1
        return self._inner.parse(content, media_type, filename)


@dataclass
class ExtractionRig:
    harness: Harness
    store: ExtractionStore
    blobs: FakeBlobReader
    parser: CountingParser
    handler: RecordExtractionHandler

    def unit_of_work(self) -> ExtractionUnitOfWork:
        return ExtractionUnitOfWork(self.harness.state, self.store)

    def command(self, document_id: UUID, version_id: UUID) -> RecordExtraction:
        return RecordExtraction(document_id=document_id, version_id=version_id)

    async def import_document(
        self,
        content: bytes,
        *,
        kind: DocumentKind = DocumentKind.COURSE,
        title: str = "Python avancé",
        filename: str = "cours.md",
        media_type: str = "text/markdown",
    ) -> RecordExtraction:
        result = await self.harness.import_document.handle(
            ImportDocument(
                title=title,
                kind=kind,
                topics=("python",),
                language="fr",
                filename=filename,
                media_type=media_type,
                content=stream(content),
            )
        )
        return self.command(result.document_id, result.version_id)


def make_rig(*, max_tokens: int = 1000) -> ExtractionRig:
    harness = make_harness(max_upload_bytes=1_000_000)
    store = ExtractionStore()
    blobs = FakeBlobReader(harness)
    parser = CountingParser()

    def factory() -> ExtractionUnitOfWork:
        return ExtractionUnitOfWork(harness.state, store)

    handler = RecordExtractionHandler(
        unit_of_work=factory,
        blob_reader=blobs,
        parser=parser,
        token_counter=COUNTER,
        policy=policy(max_tokens),
    )
    return ExtractionRig(harness, store, blobs, parser, handler)
