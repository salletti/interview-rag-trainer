"""Doubles et harnais de S4 (indexation), construits sur ceux de S2 et S3.

Les documents sont importés par les VRAIS handlers de S2, extraits par le VRAI
``RecordExtractionHandler`` de S3 (donc de vrais chunks, ids et hashes) ; seules
les frontières de S4 sont remplacées : ``Embedder`` (``HashEmbedder``,
vecteurs dérivés d'un hash), ``VectorIndex`` (``FakeVectorIndex``, en mémoire),
dépôts du corpus et des indexations, lecture des chunks. Un « garde » vérifie
qu'aucun appel d'embedding ni de Qdrant n'a lieu pendant qu'une transaction est
ouverte (règle de 002, S0 §5).

API figée par ces tests (ports du domaine, ``knowledge_base.domain.providers``) :

- ``embedder.Embedder`` : ``profile: EmbeddingProfile`` ;
  ``await embed_passages(texts: Sequence[str]) -> list[list[float]]`` ;
  ``await embed_query(text: str) -> list[float]``. Le préfixe de modèle est
  l'affaire de l'adapter : les textes reçus sont les ``contextual_text`` bruts.
- ``vector_index.VectorIndex`` (``index_id`` désigne la collection ; son nom est
  calculé par l'adapter) : ``await ensure_collection(index_id, profile)``
  (idempotent, crée aussi les index de payload) ; ``await check_collection(
  index_id, profile) -> bool`` (vrai si la collection existe avec la dimension
  et la distance du profil) ; ``await upsert(index_id, points:
  Sequence[IndexedPoint], *, wait: bool = True)`` ; ``await retrieve(index_id,
  point_ids: Sequence[UUID]) -> Sequence[tuple[UUID, str]]`` (couples
  ``(point_id, chunk_hash)`` des points EXISTANTS, sans vecteur) ; ``await
  set_visibility(index_id, *, document_id, version_id, visible)`` ; ``await
  delete_points(index_id, selector: PointSelector)``.
- ``knowledge_base.domain.value_objects.indexed_point`` : ``IndexedPoint(point_id,
  vector: tuple[float, ...], chunk_id, unit_id, version_id, document_id,
  chunk_hash, index_id, visible, topics: tuple[str, ...], kind: str, language)``
  (le payload Qdrant porte ces mêmes noms, UUID en texte) ;
  ``PointSelector(document_id, version_id: UUID | None = None)`` : les points
  d'une version, ou de tout le document si ``version_id`` est ``None``.
- ``chunk_reader.ChunkReader`` : ``await list_chunks(version_id, policy_version)
  -> tuple[DocumentChunk, ...]`` dans l'ordre (unité, rang) ; vide si le lot
  n'existe pas.
- ``knowledge_corpus_repository.KnowledgeCorpusRepository`` : ``await
  get_shared() -> KnowledgeCorpus | None`` (``SELECT ... FOR SHARE``),
  ``await get_for_update()`` (``FOR UPDATE``), ``await add(corpus)`` (lève
  ``CorpusAlreadyExistsError`` si une autre transaction l'a créé), ``await
  save(corpus)``.
- ``indexation_repository.IndexationRepository`` : ``await get(version_id,
  index_id) -> Indexation | None``, ``await add(indexation)``, ``await
  save(indexation)``.
- ``UnitOfWork`` gagne les membres ``corpus``, ``indexations`` et ``chunks``
  (dans la même transaction que ``documents`` et ``work_queue``).

Items de la work queue : ``index_version`` (dedup ``index_version:<version_id>:
<index_id>``, payload ``document_id``, ``version_id``, ``index_id``) et
``sync_visibility`` (dedup ``sync_visibility:<document_id>:<visibility_revision>``,
payload ``document_id``, ``visibility_revision``).
"""

from __future__ import annotations

import copy
import hashlib
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import TracebackType
from typing import Self
from uuid import UUID

from knowledge_base.domain.entities.indexation import Indexation
from knowledge_base.domain.entities.knowledge_corpus import KnowledgeCorpus
from knowledge_base.domain.entities.knowledge_document import (
    KnowledgeDocument,
    VersionStatus,
)
from knowledge_base.domain.providers.knowledge_corpus_repository import (
    CorpusAlreadyExistsError,
)
from knowledge_base.domain.services.point_id import point_id
from knowledge_base.domain.value_objects.document_chunk import DocumentChunk
from knowledge_base.domain.value_objects.document_kind import DocumentKind
from knowledge_base.domain.value_objects.embedding_profile import (
    Distance,
    EmbeddingProfile,
)
from knowledge_base.domain.value_objects.indexed_point import (
    IndexedPoint,
    PointSelector,
)
from knowledge_base.use_cases.complete_indexation.complete_indexation_command import (
    CompleteIndexation,
)
from knowledge_base.use_cases.complete_indexation.complete_indexation_handler import (
    CompleteIndexationHandler,
)
from knowledge_base.use_cases.import_document.import_document_command import (
    ImportDocument,
)
from knowledge_base.use_cases.import_document_version.import_document_version_command import (  # noqa: E501
    ImportDocumentVersion,
)
from knowledge_base.use_cases.provision_search_index.provision_search_index_command import (  # noqa: E501
    ProvisionSearchIndex,
)
from knowledge_base.use_cases.provision_search_index.provision_search_index_handler import (  # noqa: E501
    ProvisionSearchIndexHandler,
)
from knowledge_base.use_cases.publish_document_version.publish_document_version_handler import (  # noqa: E501
    PublishDocumentVersionHandler,
)
from knowledge_base.use_cases.record_extraction.record_extraction_command import (
    RecordExtraction,
)
from knowledge_base.use_cases.record_extraction.record_extraction_handler import (
    RecordExtractionHandler,
)
from knowledge_base.use_cases.sync_document_visibility.sync_document_visibility_handler import (  # noqa: E501
    SyncDocumentVisibilityHandler,
)
from tests.unit.knowledge_base.domain.services.support import COUNTER, policy
from tests.unit.knowledge_base.use_cases.extraction_fakes import (
    CountingParser,
    ExtractionStore,
    ExtractionUnitOfWork,
    FakeBlobReader,
    StoredBatch,
)
from tests.unit.knowledge_base.use_cases.fakes import (
    NOW,
    Harness,
    State,
    make_harness,
    stream,
)

DIMENSION = 16
PROFILE = EmbeddingProfile(
    model="test/hash-embedder",
    revision="test-1",
    dimension=DIMENSION,
    distance=Distance.COSINE,
    normalize=True,
    query_prefix="query: ",
    passage_prefix="passage: ",
    max_tokens=512,
    tokenizer="test/hash-embedder",
)
INDEXED_AT = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def hash_vector(text: str, dimension: int = DIMENSION) -> list[float]:
    """Vecteur unitaire déterministe dérivé du SHA-256 du texte."""
    raw = [
        int.from_bytes(hashlib.sha256(f"{i}:{text}".encode()).digest()[:4], "big")
        / 2**32
        - 0.5
        for i in range(dimension)
    ]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


Guard = Callable[[str], None]


def no_guard(_: str) -> None:
    return None


class HashEmbedder:
    """``Embedder`` de test : mêmes textes, mêmes vecteurs, sans modèle."""

    def __init__(
        self, profile: EmbeddingProfile = PROFILE, guard: Guard = no_guard
    ) -> None:
        self.profile = profile
        self.guard = guard
        self.passage_calls: list[list[str]] = []
        self.query_calls: list[str] = []

    def passage_vector(self, text: str) -> list[float]:
        return hash_vector(self.profile.passage_prefix + text, self.profile.dimension)

    async def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        self.guard("embed_passages")
        self.passage_calls.append(list(texts))
        return [self.passage_vector(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        self.guard("embed_query")
        self.query_calls.append(text)
        return hash_vector(self.profile.query_prefix + text, self.profile.dimension)


class WrongDimensionEmbedder(HashEmbedder):
    """Un modèle dont la sortie ne correspond pas au profil déclaré."""

    async def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        self.guard("embed_passages")
        self.passage_calls.append(list(texts))
        return [hash_vector(t, self.profile.dimension + 1) for t in texts]


class BrokenEmbedder(HashEmbedder):
    """Un modèle indisponible : toute demande d'embedding échoue."""

    async def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        raise RuntimeError("modèle indisponible")


class FakeVectorIndex:
    """Qdrant en mémoire, contrat de ``VectorIndex``. Pannes et altérations
    injectables : ``conforming``, ``after_upsert`` (altère le contenu des
    collections après chaque upsert), ``fail_upsert_on`` (numéro d'upsert, à
    partir de 1, qui lève)."""

    def __init__(self, guard: Guard = no_guard) -> None:
        self.guard = guard
        self.collections: dict[UUID, dict[UUID, IndexedPoint]] = {}
        self.profiles: dict[UUID, EmbeddingProfile] = {}
        self.conforming = True
        self.fail_ensure = False
        self.fail_upsert_on: int | None = None
        self.after_upsert: Callable[[dict[UUID, IndexedPoint]], None] | None = None
        self.calls: list[str] = []
        self.upsert_sizes: list[int] = []
        self.upsert_waits: list[bool] = []

    def points(self, index_id: UUID) -> dict[UUID, IndexedPoint]:
        return self.collections.get(index_id, {})

    async def ensure_collection(
        self, index_id: UUID, profile: EmbeddingProfile
    ) -> None:
        self.guard("ensure_collection")
        self.calls.append("ensure_collection")
        if self.fail_ensure:
            raise RuntimeError("Qdrant indisponible")
        self.collections.setdefault(index_id, {})
        self.profiles.setdefault(index_id, profile)

    async def check_collection(self, index_id: UUID, profile: EmbeddingProfile) -> bool:
        self.guard("check_collection")
        self.calls.append("check_collection")
        return self.conforming and index_id in self.collections

    async def upsert(
        self, index_id: UUID, points: Sequence[IndexedPoint], *, wait: bool = True
    ) -> None:
        self.guard("upsert")
        self.calls.append("upsert")
        self.upsert_sizes.append(len(points))
        self.upsert_waits.append(wait)
        if self.fail_upsert_on == len(self.upsert_sizes):
            raise RuntimeError("Qdrant : connexion perdue pendant l'upsert")
        collection = self.collections.setdefault(index_id, {})
        for point in points:
            collection[point.point_id] = point
        if self.after_upsert is not None:
            self.after_upsert(collection)

    async def retrieve(
        self, index_id: UUID, point_ids: Sequence[UUID]
    ) -> Sequence[tuple[UUID, str]]:
        self.guard("retrieve")
        self.calls.append("retrieve")
        collection = self.points(index_id)
        return [(i, collection[i].chunk_hash) for i in point_ids if i in collection]

    async def set_visibility(
        self, index_id: UUID, *, document_id: UUID, version_id: UUID, visible: bool
    ) -> None:
        self.guard("set_visibility")
        self.calls.append("set_visibility")
        collection = self.points(index_id)
        for key, point in list(collection.items()):
            if point.document_id == document_id and point.version_id == version_id:
                collection[key] = replace(point, visible=visible)

    async def delete_points(self, index_id: UUID, selector: PointSelector) -> None:
        self.guard("delete_points")
        self.calls.append("delete_points")
        collection = self.points(index_id)
        for key, point in list(collection.items()):
            same_version = (
                selector.version_id is None or point.version_id == selector.version_id
            )
            if point.document_id == selector.document_id and same_version:
                del collection[key]


def drop_first(collection: dict[UUID, IndexedPoint]) -> None:
    del collection[next(iter(collection))]


def corrupt_first_hash(collection: dict[UUID, IndexedPoint]) -> None:
    key = next(iter(collection))
    collection[key] = replace(collection[key], chunk_hash="0" * 64)


# --- état et transactions -------------------------------------------------------------


@dataclass
class IndexingState:
    """État « commité » du corpus et des indexations."""

    corpus: KnowledgeCorpus | None = None
    indexations: dict[tuple[UUID, UUID], Indexation] = field(default_factory=dict)
    locks: list[str] = field(default_factory=list)
    open_transactions: int = 0
    # Une autre transaction crée le corpus entre notre lecture et notre ``add``.
    race_corpus: KnowledgeCorpus | None = None


@dataclass
class Working:
    corpus: KnowledgeCorpus | None = None
    indexations: dict[tuple[UUID, UUID], Indexation] = field(default_factory=dict)


class FakeCorpusRepository:
    def __init__(self, state: IndexingState, working: Working) -> None:
        self._state = state
        self._w = working

    async def get_shared(self) -> KnowledgeCorpus | None:
        self._state.locks.append("shared")
        return self._w.corpus

    async def get_for_update(self) -> KnowledgeCorpus | None:
        self._state.locks.append("update")
        return self._w.corpus

    async def add(self, corpus: KnowledgeCorpus) -> None:
        if self._state.race_corpus is not None:
            self._state.corpus = self._state.race_corpus
            self._state.race_corpus = None
            raise CorpusAlreadyExistsError
        self._w.corpus = corpus

    async def save(self, corpus: KnowledgeCorpus) -> None:
        self._w.corpus = corpus


class FakeIndexations:
    def __init__(self, working: Working) -> None:
        self._w = working

    async def get(self, version_id: UUID, index_id: UUID) -> Indexation | None:
        return self._w.indexations.get((version_id, index_id))

    async def add(self, indexation: Indexation) -> None:
        self._w.indexations[(indexation.version_id, indexation.index_id)] = indexation

    async def save(self, indexation: Indexation) -> None:
        self._w.indexations[(indexation.version_id, indexation.index_id)] = indexation


class FakeChunks:
    """Lit les lots d'extraction « commités » de S3."""

    def __init__(self, store: ExtractionStore) -> None:
        self._store = store

    async def list_chunks(
        self, version_id: UUID, policy_version: str
    ) -> tuple[DocumentChunk, ...]:
        for batch in self._store.batches:
            if (
                batch.version_id == version_id
                and batch.policy_version == policy_version
            ):
                return batch.chunks
        return ()


class IndexingUnitOfWork(ExtractionUnitOfWork):
    """Unit of Work de S3 + ``corpus``, ``indexations``, ``chunks`` ; le corpus
    et les indexations ne sont publiés qu'au ``commit``."""

    def __init__(
        self, state: State, store: ExtractionStore, indexing: IndexingState
    ) -> None:
        super().__init__(state, store)
        self._indexing = indexing
        self._working_indexing = Working()
        self.corpus = FakeCorpusRepository(indexing, self._working_indexing)
        self.indexations = FakeIndexations(self._working_indexing)
        self.chunks = FakeChunks(store)

    async def __aenter__(self) -> Self:
        self._indexing.open_transactions += 1
        self._working_indexing.corpus = copy.deepcopy(self._indexing.corpus)
        self._working_indexing.indexations = copy.deepcopy(self._indexing.indexations)
        return await super().__aenter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._indexing.open_transactions -= 1
        return await super().__aexit__(exc_type, exc, tb)

    async def commit(self) -> None:
        await super().commit()
        self._indexing.corpus = self._working_indexing.corpus
        self._indexing.indexations = self._working_indexing.indexations


# --- harnais --------------------------------------------------------------------------


@dataclass
class IndexingRig:
    harness: Harness
    store: ExtractionStore
    indexing: IndexingState
    embedder: HashEmbedder
    vector: FakeVectorIndex
    violations: list[str]
    extract_handler: RecordExtractionHandler
    provision_handler: ProvisionSearchIndexHandler
    complete: CompleteIndexationHandler
    publish: PublishDocumentVersionHandler
    sync: SyncDocumentVisibilityHandler

    def unit_of_work(self) -> IndexingUnitOfWork:
        return IndexingUnitOfWork(self.harness.state, self.store, self.indexing)

    # --- scénario ---

    async def provision(self) -> UUID:
        """Crée et active le premier index (corpus vide)."""
        return await self.provision_handler.handle(
            ProvisionSearchIndex(profile=PROFILE, chunking_policy=policy())
        )

    def add_building_index(self, index_id: UUID) -> None:
        """Ajoute un index ``BUILDING`` directement dans l'état commité (la
        migration blue/green complète est celle de S9)."""
        corpus = self.indexing.corpus
        assert corpus is not None
        corpus.provision_index(
            index_id=index_id,
            profile=PROFILE,
            chunking_policy=policy(),
            now=NOW,
        )
        self.vector.collections.setdefault(index_id, {})
        self.vector.profiles[index_id] = PROFILE

    async def import_document(
        self,
        content: bytes,
        *,
        kind: DocumentKind = DocumentKind.QA_SHEET,
        title: str = "Fiche FastAPI",
        filename: str = "fiche.md",
    ) -> RecordExtraction:
        result = await self.harness.import_document.handle(
            ImportDocument(
                title=title,
                kind=kind,
                topics=("python",),
                language="fr",
                filename=filename,
                media_type="text/markdown",
                content=stream(content),
            )
        )
        return RecordExtraction(
            document_id=result.document_id, version_id=result.version_id
        )

    async def import_next_version(
        self, document_id: UUID, content: bytes
    ) -> RecordExtraction:
        result = await self.harness.import_version.handle(
            ImportDocumentVersion(
                document_id=document_id,
                filename="fiche.md",
                media_type="text/markdown",
                content=stream(content),
            )
        )
        return RecordExtraction(document_id=document_id, version_id=result.version_id)

    async def extract(self, command: RecordExtraction) -> None:
        """Finalisation de ``extract_version`` : ``EXTRACTED`` + indexations."""
        prepared = await self.extract_handler.prepare(command)
        assert prepared is not None
        async with self.unit_of_work() as uow:
            assert await self.extract_handler.apply(uow, prepared)
            await uow.commit()

    async def index(
        self, command: RecordExtraction, index_id: UUID, *, commit: bool = True
    ) -> bool:
        """``index_version`` de bout en bout : vrai si la transaction de
        finalisation a écrit quelque chose (``apply`` vrai)."""
        request = self.complete_command(command, index_id)
        prepared = await self.complete.prepare(request)
        if prepared is None:
            return False
        async with self.unit_of_work() as uow:
            applied = await self.complete.apply(uow, prepared)
            if commit:
                await uow.commit()
        return applied

    @staticmethod
    def complete_command(
        command: RecordExtraction, index_id: UUID
    ) -> CompleteIndexation:
        return CompleteIndexation(
            document_id=command.document_id,
            version_id=command.version_id,
            index_id=index_id,
        )

    # --- lectures de l'état commité ---

    def document(self, document_id: UUID) -> KnowledgeDocument:
        return self.harness.state.documents[document_id]

    def status(self, command: RecordExtraction) -> VersionStatus:
        return self.document(command.document_id).get_version(command.version_id).status

    def indexation(self, command: RecordExtraction, index_id: UUID) -> Indexation:
        return self.indexing.indexations[(command.version_id, index_id)]

    def chunks_of(self, command: RecordExtraction) -> tuple[DocumentChunk, ...]:
        batches: list[StoredBatch] = [
            b for b in self.store.batches if b.version_id == command.version_id
        ]
        (batch,) = batches
        return batch.chunks

    def expected_points(self, command: RecordExtraction, index_id: UUID) -> set[UUID]:
        return {point_id(index_id, c.chunk_id) for c in self.chunks_of(command)}

    def points_of(
        self, command: RecordExtraction, index_id: UUID
    ) -> list[IndexedPoint]:
        return [
            p
            for p in self.vector.points(index_id).values()
            if p.version_id == command.version_id
        ]

    def sync_keys(self) -> list[str]:
        return sorted(
            k for k in self.harness.state.items if k.startswith("sync_visibility:")
        )

    def mark_completed(self, command: RecordExtraction, index_id: UUID) -> None:
        """Pose ``COMPLETED`` dans l'état commité sans passer par la
        publication : isole ``PublishDocumentVersion`` de ``CompleteIndexation``."""
        self.indexation(command, index_id).complete(
            manifest_digest="ab" * 32, point_count=1, completed_at=INDEXED_AT
        )


def make_indexing_rig(
    *,
    batch_size: int = 32,
    max_tokens: int = 1000,
    embedder: HashEmbedder | None = None,
) -> IndexingRig:
    harness = make_harness(max_upload_bytes=1_000_000)
    store = ExtractionStore()
    indexing = IndexingState()
    violations: list[str] = []

    def guard(operation: str) -> None:
        if indexing.open_transactions:
            violations.append(operation)

    embedder = embedder or HashEmbedder()
    embedder.guard = guard
    vector = FakeVectorIndex(guard=guard)
    ids = iter(range(9001, 9100))

    def factory() -> IndexingUnitOfWork:
        return IndexingUnitOfWork(harness.state, store, indexing)

    publish = PublishDocumentVersionHandler(clock=lambda: INDEXED_AT)
    return IndexingRig(
        harness=harness,
        store=store,
        indexing=indexing,
        embedder=embedder,
        vector=vector,
        violations=violations,
        extract_handler=RecordExtractionHandler(
            unit_of_work=factory,
            blob_reader=FakeBlobReader(harness),
            parser=CountingParser(),
            token_counter=COUNTER,
            policy=policy(max_tokens),
        ),
        provision_handler=ProvisionSearchIndexHandler(
            unit_of_work=factory,
            vector_index=vector,
            id_factory=lambda: UUID(int=next(ids)),
            clock=lambda: NOW,
        ),
        complete=CompleteIndexationHandler(
            unit_of_work=factory,
            embedder=embedder,
            vector_index=vector,
            publisher=publish,
            clock=lambda: INDEXED_AT,
            batch_size=batch_size,
        ),
        publish=publish,
        sync=SyncDocumentVisibilityHandler(unit_of_work=factory, vector_index=vector),
    )


def qa_sheet(questions: int = 2, *, tag: str = "") -> bytes:
    """Fiche Q/R de ``questions`` questions, donc ``questions`` chunks."""
    parts = ["# Fiche FastAPI\n"]
    for n in range(questions):
        parts.append(
            f"## Question : Qu'est-ce que la notion {tag}{n} ?\n\n"
            f"La notion {tag}{n} sert à structurer le code.\n"
        )
    return "\n".join(parts).encode()
