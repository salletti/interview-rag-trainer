"""Doubles et harnais des tests rouges de S9 (migration blue/green d'index) :
construits sur ceux de S4 (``indexing_fakes``), sans I/O.

Les contrats NOUVEAUX de S9 sont chargés PAREILLEMENT à la demande par
``contract(module, nom)`` : tant qu'un module ou un nom n'existe pas, le test
qui en dépend échoue avec le message « contrat S9 absent : ... » (et non avec
une ``ImportError`` de collection qui interromprait toute la session). Une fois
les modules créés, ces chargements peuvent être remplacés par des imports
directs.

Contrats supposés (chemins sous ``knowledge_base``) :

Domaine, ports (``domain/providers``) :

- ``embedder_provider`` : ``TokenCountingEmbedder`` (``Embedder`` + propriété
  ``token_counter``), ``EmbedderProvider.for_profile(profile) ->
  TokenCountingEmbedder`` (synchrone, ne charge rien de lourd : l'``Embedder``
  rendu est paresseux) et ``UnknownEmbeddingProfileError(profile)`` quand le
  processus ne sait pas charger ce profil.
- ``index_migration`` : ``IndexableVersion(document_id, version_id,
  version_status, policy_versions: frozenset[str])`` et
  ``IndexableVersionReader.list_indexable_versions()`` : les versions
  ``PUBLISHED`` ou ``EXTRACTED`` des documents ``ACTIVE``, avec les politiques
  de leurs lots de chunks ; ``MigrationUnitOfWork`` = ``IndexingUnitOfWork`` +
  membre ``versions`` (lu dans la même transaction que le verrou du corpus).
- ``index_collections`` : ``IndexCollections.delete_collection(index_id)`` :
  supprime la collection de l'index, sans erreur si elle n'existe plus. Port
  ÉTROIT, distinct de ``VectorIndex`` : l'élargir casserait ``mypy`` sur les
  onze doubles de ``VectorIndex`` des tests de S4 à S8 sans rien leur
  apporter ; ``QdrantVectorIndex`` implémente les deux.

Cas d'usage (``use_cases``), un paquet par commande, même forme que S4 :

- ``start_index_migration`` : ``StartIndexMigration(profile, chunking_policy)``,
  ``StartIndexMigrationHandler(*, unit_of_work, vector_index, id_factory,
  clock).handle(command) -> UUID`` (nouvel index ``BUILDING``) ; erreurs dans
  ``start_index_migration.errors``.
- ``activate_search_index`` : ``ActivateSearchIndex(index_id)``,
  ``ActivateSearchIndexHandler(*, unit_of_work, clock).handle(command)``.
- ``drop_index_collection`` : ``DropIndexCollection(index_id)``,
  ``DropIndexCollectionHandler(*, unit_of_work, collections).handle(command)``.
- ``rechunk_version`` : ``RechunkVersion(document_id, version_id, index_id)``,
  ``RechunkVersionHandler(*, unit_of_work, blob_reader, parser, embedders,
  max_document_tokens=None)`` avec ``prepare(command)``, ``apply(transaction,
  prepared)``, ``fail(transaction, command, reason)``.
- ``CompleteIndexationHandler(..., embedders=<EmbedderProvider>)`` et
  ``SearchKnowledgeHandler(..., embedders=<EmbedderProvider>)`` : seule forme
  depuis la revue S9 (``embedder=`` et ``token_counter=`` supprimés).

Items de la work queue (``kind``, clé de dédoublonnage, payload en texte) :
``rechunk_version`` (``rechunk_version:<version_id>:<index_id>``,
``document_id``, ``version_id``, ``index_id``) et ``drop_index_collection``
(``drop_index_collection:<index_id>``, ``index_id``).
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, cast
from uuid import UUID

import pytest

from knowledge_base.domain.entities.indexation import Indexation
from knowledge_base.domain.entities.knowledge_corpus import IndexStatus, SearchIndex
from knowledge_base.domain.entities.knowledge_document import (
    DocumentStatus,
    VersionStatus,
)
from knowledge_base.domain.value_objects.chunking_policy import ChunkingPolicy
from knowledge_base.domain.value_objects.document_chunk import DocumentChunk
from knowledge_base.domain.value_objects.embedding_profile import EmbeddingProfile
from knowledge_base.domain.value_objects.indexed_point import (
    IndexedPoint,
    PointSelector,
)
from knowledge_base.use_cases.complete_indexation.complete_indexation_handler import (
    CompleteIndexationHandler,
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
from tests.unit.knowledge_base.domain.services.support import COUNTER, policy, words
from tests.unit.knowledge_base.use_cases.extraction_fakes import (
    CountingParser,
    ExtractionStore,
    FakeBlobReader,
)
from tests.unit.knowledge_base.use_cases.fakes import NOW
from tests.unit.knowledge_base.use_cases.indexing_fakes import (
    PROFILE,
    FakeVectorIndex,
    Guard,
    HashEmbedder,
    IndexingRig,
    IndexingState,
    IndexingUnitOfWork,
    make_indexing_rig,
)

# --- chargement des contrats ----------------------------------------------------------

KB = "knowledge_base"
EMBEDDER_PROVIDER = f"{KB}.domain.providers.embedder_provider"
INDEX_MIGRATION_PORT = f"{KB}.domain.providers.index_migration"
START = f"{KB}.use_cases.start_index_migration"
ACTIVATE = f"{KB}.use_cases.activate_search_index"
ABANDON = f"{KB}.use_cases.abandon_index_migration"
DROP = f"{KB}.use_cases.drop_index_collection"
RECHUNK = f"{KB}.use_cases.rechunk_version"


def contract(module: str, name: str) -> Any:
    """``module.name``, ou un échec explicite « contrat S9 absent »."""
    try:
        loaded = importlib.import_module(module)
    except ModuleNotFoundError as error:
        missing = error.name or ""
        if module == missing or module.startswith(f"{missing}."):
            pytest.fail(f"contrat S9 absent : module {module} à créer", pytrace=False)
        raise
    try:
        return getattr(loaded, name)
    except AttributeError:
        pytest.fail(f"contrat S9 absent : {module}.{name} à créer", pytrace=False)


def start_command(profile: EmbeddingProfile, chunking_policy: ChunkingPolicy) -> Any:
    command = contract(f"{START}.start_index_migration_command", "StartIndexMigration")
    return command(profile=profile, chunking_policy=chunking_policy)


def start_error(name: str) -> type[Exception]:
    return cast(type[Exception], contract(f"{START}.errors", name))


def activate_command(index_id: UUID) -> Any:
    command = contract(
        f"{ACTIVATE}.activate_search_index_command", "ActivateSearchIndex"
    )
    return command(index_id=index_id)


def abandon_command(index_id: UUID) -> Any:
    command = contract(
        f"{ABANDON}.abandon_index_migration_command", "AbandonIndexMigration"
    )
    return command(index_id=index_id)


def drop_command(index_id: UUID) -> Any:
    command = contract(f"{DROP}.drop_index_collection_command", "DropIndexCollection")
    return command(index_id=index_id)


def rechunk_command(extraction: RecordExtraction, index_id: UUID) -> Any:
    command = contract(f"{RECHUNK}.rechunk_version_command", "RechunkVersion")
    return command(
        document_id=extraction.document_id,
        version_id=extraction.version_id,
        index_id=index_id,
    )


def unknown_profile_error() -> type[Exception]:
    return cast(
        type[Exception], contract(EMBEDDER_PROVIDER, "UnknownEmbeddingProfileError")
    )


# --- profils et politiques de test ---------------------------------------------------

# Second profil léger : autre modèle, autre dimension, autre budget. Aucun
# poids à télécharger (``HashEmbedder``).
PROFILE_B = replace(
    PROFILE,
    model="test/hash-embedder-b",
    revision="test-2",
    dimension=24,
    tokenizer="test/hash-embedder-b",
    max_tokens=256,
)
PROFILE_C = replace(
    PROFILE, model="test/hash-embedder-c", revision="test-3", dimension=32
)
POLICY_A = policy()  # politique de l'index initial (identité chunking-test-1+…)
# Même politique que l'index actif : les lots existants servent tels quels.
POLICY_SAME = POLICY_A
# Autre budget, donc autre lot de chunks : rechunking.
POLICY_SHORT = policy(60, version="chunking-test-2")
POLICY_C = policy(40, version="chunking-test-3")
LATER = NOW + timedelta(hours=3)
# Délai de suppression d'une collection retirée : nul dans les tests, pour que
# l'item soit disponible tout de suite (la production passe
# ``KB_INDEX_DROP_DELAY_SECONDS``).
NO_DELAY = timedelta(0)

NEW_INDEX = UUID(int=8_001)
THIRD_INDEX = UUID(int=8_002)


def long_sheet(tag: str = "") -> bytes:
    """Une question, six paragraphes de 12 mots : un seul chunk sous
    ``POLICY_A`` (1000 mots), plusieurs sous ``POLICY_SHORT`` (60 mots)."""
    body = "\n\n".join(words(12, prefix=f"p{tag}{n}x") for n in range(6))
    return (
        f"# Fiche FastAPI\n\n## Question : Qu'est-ce que la notion {tag} ?\n\n{body}\n"
    ).encode()


# --- doubles --------------------------------------------------------------------------


class S9VectorIndex(FakeVectorIndex):
    """``FakeVectorIndex`` + ``delete_collection`` (contrat S9 du port)."""

    def __init__(self, guard: Guard) -> None:
        super().__init__(guard=guard)
        self.dropped: list[UUID] = []
        # (opération, index) de chaque écriture : qui a été touché, et quand.
        self.touched: list[tuple[str, UUID]] = []
        self.fail_drop = False

    async def upsert(
        self, index_id: UUID, points: Sequence[IndexedPoint], *, wait: bool = True
    ) -> None:
        self.touched.append(("upsert", index_id))
        await super().upsert(index_id, points, wait=wait)

    async def set_visibility(
        self, index_id: UUID, *, document_id: UUID, version_id: UUID, visible: bool
    ) -> None:
        self.touched.append(("set_visibility", index_id))
        await super().set_visibility(
            index_id, document_id=document_id, version_id=version_id, visible=visible
        )

    async def delete_points(self, index_id: UUID, selector: PointSelector) -> None:
        self.touched.append(("delete_points", index_id))
        await super().delete_points(index_id, selector)

    async def delete_collection(self, index_id: UUID) -> None:
        self.guard("delete_collection")
        self.calls.append("delete_collection")
        if self.fail_drop:
            raise RuntimeError("Qdrant indisponible")
        self.collections.pop(index_id, None)
        self.profiles.pop(index_id, None)
        self.dropped.append(index_id)


class ProfileModel(HashEmbedder):
    """Un ``TokenCountingEmbedder`` de test : un token par mot."""

    token_counter = COUNTER


class FakeEmbedderProvider:
    """``EmbedderProvider`` en mémoire : les profils connus sont ceux des
    modèles fournis ; ``requested`` garde l'ordre des demandes."""

    def __init__(self, *models: ProfileModel) -> None:
        self.models = {m.profile: m for m in models}
        self.requested: list[EmbeddingProfile] = []

    def for_profile(self, profile: EmbeddingProfile) -> ProfileModel:
        self.requested.append(profile)
        try:
            return self.models[profile]
        except KeyError:
            raise unknown_profile_error()(profile) from None


@dataclass(frozen=True)
class _Version:
    """Même forme que ``IndexableVersion`` (chargé à la demande)."""

    document_id: UUID
    version_id: UUID
    version_status: VersionStatus
    policy_versions: frozenset[str]


class FakeVersions:
    """``IndexableVersionReader`` : lit les documents de la transaction en
    cours et les lots de chunks commités.

    Documents à synchronisation de visibilité en attente, ou dont la dernière
    a échoué : ceux que le test pose dans ``IndexingState.awaiting_visibility``
    et ``IndexingState.failed_visibility`` (aucun par défaut). Le
    harnais n'exécute jamais les items ``sync_visibility`` : les déduire de la
    work queue rendrait toute activation impossible ici ; la garde est
    vérifiée par
    ``test_a_pending_visibility_sync_blocks_the_activation`` et, sur
    PostgreSQL, par l'intégration."""

    def __init__(self, unit: S9UnitOfWork) -> None:
        self._unit = unit

    async def documents_awaiting_visibility(self) -> frozenset[UUID]:
        return self._unit.awaiting_visibility()

    async def documents_with_failed_visibility(self) -> frozenset[UUID]:
        return self._unit.failed_visibility()

    async def indexation_statuses(self, index_id: UUID) -> dict[UUID, Any]:
        return {
            version_id: indexation.status
            for (version_id, in_index), indexation in self._unit.working_indexations()
            if in_index == index_id
        }

    async def list_indexable_versions(self) -> Sequence[Any]:
        make = contract(INDEX_MIGRATION_PORT, "IndexableVersion")
        found = []
        for document in self._unit.working_documents():
            if document.status is not DocumentStatus.ACTIVE:
                continue
            for version in document.versions:
                if version.status not in {
                    VersionStatus.PUBLISHED,
                    VersionStatus.EXTRACTED,
                }:
                    continue
                found.append(
                    make(
                        document_id=document.document_id,
                        version_id=version.version_id,
                        version_status=version.status,
                        policy_versions=self._unit.policies_of(version.version_id),
                    )
                )
        return found


class S9UnitOfWork(IndexingUnitOfWork):
    """Unit of Work de S4 + ``versions`` (le ``MigrationUnitOfWork``)."""

    def __init__(
        self, rig_state: Any, store: ExtractionStore, indexing: IndexingState
    ) -> None:
        super().__init__(rig_state, store, indexing)
        self._store = store
        self._indexing_state = indexing
        self.versions = FakeVersions(self)

    def awaiting_visibility(self) -> frozenset[UUID]:
        return self._indexing_state.awaiting_visibility

    def failed_visibility(self) -> frozenset[UUID]:
        return self._indexing_state.failed_visibility

    def working_indexations(self) -> list[tuple[tuple[UUID, UUID], Indexation]]:
        return list(self._working_indexing.indexations.items())

    def working_documents(self) -> list[Any]:
        return list(self._working.documents.values())

    def policies_of(self, version_id: UUID) -> frozenset[str]:
        return frozenset(
            b.policy_version for b in self._store.batches if b.version_id == version_id
        )


# --- harnais --------------------------------------------------------------------------


@dataclass
class S9Rig(IndexingRig):
    provider: FakeEmbedderProvider
    model_a: ProfileModel
    model_b: ProfileModel
    ids: Callable[[], UUID]
    blob_reader: FakeBlobReader
    parser: CountingParser

    @property
    def qdrant(self) -> S9VectorIndex:
        return cast(S9VectorIndex, self.vector)

    def unit_of_work(self) -> S9UnitOfWork:
        return S9UnitOfWork(self.harness.state, self.store, self.indexing)

    # --- handlers de S9 (construits à la demande) ---

    def start_handler(self, vector: Any = None) -> Any:
        handler = contract(
            f"{START}.start_index_migration_handler", "StartIndexMigrationHandler"
        )
        return handler(
            unit_of_work=self.unit_of_work,
            vector_index=vector or self.vector,
            id_factory=self.ids,
            clock=lambda: NOW,
        )

    def activate_handler(self) -> Any:
        handler = contract(
            f"{ACTIVATE}.activate_search_index_handler", "ActivateSearchIndexHandler"
        )
        return handler(
            unit_of_work=self.unit_of_work, clock=lambda: LATER, drop_delay=NO_DELAY
        )

    def abandon_handler(self, drop_delay: timedelta = timedelta(0)) -> Any:
        handler = contract(
            f"{ABANDON}.abandon_index_migration_handler",
            "AbandonIndexMigrationHandler",
        )
        return handler(
            unit_of_work=self.unit_of_work, clock=lambda: LATER, drop_delay=drop_delay
        )

    def drop_handler(self) -> Any:
        handler = contract(
            f"{DROP}.drop_index_collection_handler", "DropIndexCollectionHandler"
        )
        return handler(unit_of_work=self.unit_of_work, collections=self.vector)

    def rechunk_handler(self, **options: Any) -> Any:
        handler = contract(
            f"{RECHUNK}.rechunk_version_handler", "RechunkVersionHandler"
        )
        return handler(
            unit_of_work=self.unit_of_work,
            blob_reader=self.blob_reader,
            parser=self.parser,
            embedders=self.provider,
            **options,
        )

    # --- scénario ---

    async def start(
        self,
        profile: EmbeddingProfile = PROFILE_B,
        chunking_policy: ChunkingPolicy = POLICY_SAME,
    ) -> UUID:
        return cast(
            UUID,
            await self.start_handler().handle(start_command(profile, chunking_policy)),
        )

    async def activate(self, index_id: UUID) -> None:
        await self.activate_handler().handle(activate_command(index_id))

    async def abandon(self, index_id: UUID) -> None:
        await self.abandon_handler().handle(abandon_command(index_id))

    async def publish_document(
        self, content: bytes, index_id: UUID | None = None
    ) -> RecordExtraction:
        """Importé, extrait, indexé dans l'index ACTIF (donc publié)."""
        index_id = index_id or self.active_id()
        command = await self.import_document(content)
        await self.extract(command)
        assert await self.index(command, index_id)
        assert self.status(command) is VersionStatus.PUBLISHED
        return command

    def published_commands(self) -> list[RecordExtraction]:
        """Une commande par version actuellement publiée d'un document actif."""
        return [
            RecordExtraction(d.document_id, d.published_version_id)
            for d in self.harness.state.documents.values()
            if d.status is DocumentStatus.ACTIVE and d.published_version_id is not None
        ]

    async def index_published_into(self, index_id: UUID) -> None:
        """Exécute l'indexation de chaque version publiée dans ``index_id``."""
        for command in self.published_commands():
            assert await self.index(command, index_id)

    async def extract_only(self, content: bytes) -> RecordExtraction:
        command = await self.import_document(content)
        await self.extract(command)
        return command

    # --- lectures ---

    def corpus(self) -> Any:
        assert self.indexing.corpus is not None
        return self.indexing.corpus

    def active_id(self) -> UUID:
        active = self.corpus().active_index_id
        assert active is not None
        return cast(UUID, active)

    def index_of(self, index_id: UUID) -> SearchIndex:
        return cast(SearchIndex, self.corpus().get_index(index_id))

    def statuses(self) -> dict[UUID, IndexStatus]:
        return {i.index_id: i.status for i in self.corpus().indexes}

    def items_of_kind(self, kind: str) -> dict[str, Any]:
        return {k: i for k, i in self.harness.state.items.items() if i.kind == kind}

    def batches_of(self, command: RecordExtraction) -> dict[str, tuple[Any, ...]]:
        return {
            b.policy_version: b.chunks
            for b in self.store.batches
            if b.version_id == command.version_id
        }

    def chunks_for(
        self, command: RecordExtraction, chunking_policy: ChunkingPolicy
    ) -> tuple[DocumentChunk, ...]:
        return cast(
            tuple[DocumentChunk, ...],
            self.batches_of(command)[chunking_policy.identity],
        )

    def indexation_key(
        self, command: RecordExtraction, index_id: UUID
    ) -> tuple[UUID, UUID]:
        return (command.version_id, index_id)


def make_s9_rig(*, max_tokens: int = 1000) -> S9Rig:
    """Rig de S4 dont le moteur vectoriel sait supprimer une collection, dont
    l'Unit of Work expose ``versions`` et dont l'indexation choisit
    l'``Embedder`` par profil (``model_a`` pour ``PROFILE``, ``model_b`` pour
    ``PROFILE_B``)."""
    base = make_indexing_rig(max_tokens=max_tokens)
    guard = base.vector.guard
    vector = S9VectorIndex(guard)
    model_a, model_b = ProfileModel(PROFILE, guard), ProfileModel(PROFILE_B, guard)
    provider = FakeEmbedderProvider(model_a, model_b)
    counter = iter(range(8_001, 8_100))
    blob_reader = FakeBlobReader(base.harness)
    parser = CountingParser()
    publish = PublishDocumentVersionHandler(clock=lambda: NOW)
    state, store, indexing = base.harness.state, base.store, base.indexing

    def factory() -> S9UnitOfWork:
        return S9UnitOfWork(state, store, indexing)

    extract_handler = RecordExtractionHandler(
        unit_of_work=factory,
        blob_reader=blob_reader,
        parser=parser,
        token_counter=COUNTER,
        policy=policy(max_tokens),
    )
    provision = ProvisionSearchIndexHandler(
        unit_of_work=factory,
        vector_index=vector,
        id_factory=lambda: UUID(int=9_001),
        clock=lambda: NOW,
    )
    return S9Rig(
        harness=base.harness,
        store=store,
        indexing=indexing,
        embedder=model_a,
        vector=vector,
        violations=base.violations,
        extract_handler=extract_handler,
        provision_handler=provision,
        complete=CompleteIndexationHandler(
            unit_of_work=factory,
            vector_index=vector,
            publisher=publish,
            clock=lambda: NOW,
            batch_size=32,
            embedders=provider,
        ),
        publish=publish,
        sync=SyncDocumentVisibilityHandler(unit_of_work=factory, vector_index=vector),
        provider=provider,
        model_a=model_a,
        model_b=model_b,
        ids=lambda: UUID(int=next(counter)),
        blob_reader=blob_reader,
        parser=parser,
    )
