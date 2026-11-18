"""Doubles et harnais de S7 (versions concurrentes, retrait, purge), construits
sur ceux de S2 à S4 (``fakes``, ``extraction_fakes``, ``indexing_fakes``).

Les documents sont importés, extraits, indexés et publiés par les VRAIS
handlers ; seules les frontières nouvelles de S7 sont remplacées :

- ``DocumentBlobEraser.delete(content_hash) -> bool`` (vrai si un fichier a
  été supprimé, faux s'il était déjà absent ; jamais d'erreur pour un absent) ;
- ``DocumentBlobInventory.list_blobs()`` -> séquence d'objets portant
  ``content_hash: ContentHash`` et ``modified_at: datetime`` (aware) ;
- ``BlobReferences.holders(content_hash) -> frozenset[UUID]`` : identifiants des
  documents dont AU MOINS UNE version (quel que soit son état, tombstone
  comprise) porte cette empreinte ET dont la purge n'est pas enregistrée
  (``purged_at`` nul) : un tombstone déjà purgé ne retient plus son blob ;
- ``DocumentPurger.delete_derived_data(document_id, *, purged_at)`` : supprime
  les unités et chunks de toutes les versions du document et enregistre
  ``purged_at`` ; répétable sans effet. Exposé par la Unit of Work de la
  purge (``uow.purger``), publié seulement au ``commit``.

Les imports des modules de production à écrire (handlers de S7) sont FAITS
DANS LES FABRIQUES : un test qui n'a pas besoin de ces modules (l'import de
N+1 qui annule les indexations de N) échoue sur son assertion, pas sur un
``ModuleNotFoundError``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import TYPE_CHECKING, Self
from uuid import UUID

from knowledge_base.domain.value_objects.content_hash import ContentHash
from knowledge_base.use_cases.import_document_version.import_document_version_command import (  # noqa: E501
    ImportDocumentVersion,
)
from knowledge_base.use_cases.import_document_version.import_document_version_handler import (  # noqa: E501
    ImportDocumentVersionHandler,
)
from knowledge_base.use_cases.record_extraction.record_extraction_command import (
    RecordExtraction,
)
from tests.unit.knowledge_base.use_cases.extraction_fakes import ExtractionStore
from tests.unit.knowledge_base.use_cases.fakes import (
    NOW,
    InMemoryBlobStore,
    State,
    stream,
)
from tests.unit.knowledge_base.use_cases.indexing_fakes import (
    IndexingRig,
    IndexingState,
    IndexingUnitOfWork,
    make_indexing_rig,
    qa_sheet,
)

if TYPE_CHECKING:
    from knowledge_base.use_cases.purge_document.purge_document_handler import (
        PurgeDocumentHandler,
    )
    from knowledge_base.use_cases.withdraw_document.withdraw_document_handler import (
        WithdrawDocumentHandler,
    )

PURGED_AT = datetime(2026, 11, 19, 8, 0, tzinfo=UTC)
GRACE_BASE = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


# --- blobs ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BlobInfo:
    content_hash: ContentHash
    modified_at: datetime


class FakeBlobs:
    """Eraser et inventaire sur le ``InMemoryBlobStore`` du harnais : ce que les
    imports ont déposé est exactement ce que la purge peut supprimer."""

    def __init__(self, store: InMemoryBlobStore) -> None:
        self._store = store
        self.modified: dict[str, datetime] = {}
        self.delete_calls: list[str] = []

    @property
    def present(self) -> set[str]:
        return set(self._store.blobs)

    def drop_file(
        self, digest: str, data: bytes = b"orphelin", *, at: datetime
    ) -> None:
        """Un fichier du volume, sans version qui le référence."""
        self._store.blobs[digest] = data
        self.modified[digest] = at

    async def delete(self, content_hash: ContentHash) -> bool:
        self.delete_calls.append(content_hash.value)
        return self._store.blobs.pop(content_hash.value, None) is not None

    async def list_blobs(self) -> Sequence[BlobInfo]:
        return [
            BlobInfo(ContentHash(digest), self.modified.get(digest, NOW))
            for digest in sorted(self._store.blobs)
        ]


class FakeReferences:
    """``BlobReferences`` lu dans l'état commité des documents ; ``extra``
    ajoute un détenteur que la base, qui garantit ``UNIQUE(content_hash)``,
    ne produirait pas : le contrat doit tenir si la contrainte était levée."""

    def __init__(self, state: State, ledger: PurgeLedger) -> None:
        self._state = state
        self._ledger = ledger
        self.extra: dict[str, set[UUID]] = {}

    def also_held_by(self, content_hash: ContentHash, document_id: UUID) -> None:
        self.extra.setdefault(content_hash.value, set()).add(document_id)

    async def holders(self, content_hash: ContentHash) -> frozenset[UUID]:
        held = {
            document.document_id
            for document in self._state.documents.values()
            if document.document_id not in self._ledger.purged_at
            for version in document.versions
            if version.source.content_hash == content_hash
        }
        return frozenset(held | self.extra.get(content_hash.value, set()))


# --- purge des données dérivées ------------------------------------------------------


@dataclass
class PurgeLedger:
    purged_at: dict[UUID, datetime] = field(default_factory=dict)


class FakePurger:
    def __init__(self, pending: list[tuple[UUID, datetime]]) -> None:
        self._pending = pending

    async def delete_derived_data(
        self, document_id: UUID, *, purged_at: datetime
    ) -> None:
        self._pending.append((document_id, purged_at))


class PurgeUnitOfWork(IndexingUnitOfWork):
    """Unit of Work d'indexation + ``purger`` ; unités, chunks et ``purged_at``
    ne disparaissent / n'apparaissent qu'au ``commit``."""

    def __init__(
        self,
        state: State,
        store: ExtractionStore,
        indexing: IndexingState,
        ledger: PurgeLedger,
    ) -> None:
        super().__init__(state, store, indexing)
        self._shared_state = state
        self._shared_store = store
        self._ledger = ledger
        self._purges: list[tuple[UUID, datetime]] = []
        self.purger = FakePurger(self._purges)

    async def __aenter__(self) -> Self:
        self._purges.clear()
        return await super().__aenter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._purges.clear()
        return await super().__aexit__(exc_type, exc, tb)

    async def commit(self) -> None:
        await super().commit()
        for document_id, purged_at in self._purges:
            document = self._shared_state.documents.get(document_id)
            versions = (
                set() if document is None else {v.version_id for v in document.versions}
            )
            self._shared_store.batches[:] = [
                b for b in self._shared_store.batches if b.version_id not in versions
            ]
            self._ledger.purged_at[document_id] = purged_at
        self._purges.clear()


# --- harnais -------------------------------------------------------------------------


@dataclass
class S7Rig:
    base: IndexingRig
    blobs: FakeBlobs
    references: FakeReferences
    ledger: PurgeLedger

    # --- fabriques (imports différés : modules de S7 à écrire) ---

    def unit_of_work(self) -> IndexingUnitOfWork:
        return self.base.unit_of_work()

    def purge_unit_of_work(self) -> PurgeUnitOfWork:
        return PurgeUnitOfWork(
            self.base.harness.state, self.base.store, self.base.indexing, self.ledger
        )

    def import_version_handler(self) -> ImportDocumentVersionHandler:
        """Le VRAI handler d'import de version, sur la Unit of Work d'indexation
        (celle de production expose ``corpus`` et ``indexations``)."""
        return ImportDocumentVersionHandler(
            unit_of_work=self.unit_of_work,
            blob_store=self.base.harness.blobs,
            max_upload_bytes=1_000_000,
            clock=lambda: NOW,
        )

    def withdraw_handler(self) -> WithdrawDocumentHandler:
        from knowledge_base.use_cases.withdraw_document.withdraw_document_handler import (  # noqa: E501
            WithdrawDocumentHandler,
        )

        return WithdrawDocumentHandler(unit_of_work=self.unit_of_work)

    def purge_handler(self) -> PurgeDocumentHandler:
        from knowledge_base.use_cases.purge_document.purge_document_handler import (
            PurgeDocumentHandler,
        )

        return PurgeDocumentHandler(
            unit_of_work=self.purge_unit_of_work,
            vector_index=self.base.vector,
            blob_eraser=self.blobs,
            blob_references=self.references,
            clock=lambda: PURGED_AT,
        )

    # --- scénario ---

    async def import_next(self, document_id: UUID, content: bytes) -> RecordExtraction:
        result = await self.import_version_handler().handle(
            ImportDocumentVersion(
                document_id=document_id,
                filename="fiche.md",
                media_type="text/markdown",
                content=stream(content),
            )
        )
        return RecordExtraction(document_id=document_id, version_id=result.version_id)

    async def published(
        self, index_id: UUID, content: bytes | None = None, *, title: str = "Fiche"
    ) -> RecordExtraction:
        """Version importée, extraite, indexée donc publiée (points invisibles)."""
        command = await self.base.import_document(content or qa_sheet(), title=title)
        await self.base.extract(command)
        await self.base.index(command, index_id)
        return command

    def mark_withdrawn(self, document_id: UUID) -> None:
        """Retrait posé directement sur l'état commité : les tests de la purge
        ne dépendent pas de ``WithdrawDocument``."""
        self.base.document(document_id).withdraw()

    async def run_purge(
        self, document_id: UUID, handler: PurgeDocumentHandler | None = None
    ) -> bool:
        """``prepare`` (effets externes) puis ``apply`` dans la transaction de
        finalisation, comme le fait le handler de work item. Faux si ``prepare``
        n'a rien à faire."""
        from knowledge_base.use_cases.purge_document.purge_document_command import (
            PurgeDocument,
        )

        use_case = handler or self.purge_handler()
        command = PurgeDocument(document_id=document_id)
        prepared = await use_case.prepare(command)
        if prepared is None:
            return False
        async with self.purge_unit_of_work() as uow:
            await use_case.apply(uow, prepared)
            await uow.commit()
        return True

    # --- lectures de l'état commité ---

    def versions_with_chunks(self) -> set[UUID]:
        return {batch.version_id for batch in self.base.store.batches}

    def points_of_document(self, document_id: UUID, index_id: UUID) -> list[UUID]:
        return [
            point_id
            for point_id, point in self.base.vector.points(index_id).items()
            if point.document_id == document_id
        ]

    def item(self, key: str) -> str | None:
        found = self.base.harness.state.items.get(key)
        return None if found is None else found.status

    def items_of_kind(self, kind: str) -> list[str]:
        return sorted(
            key
            for key, found in self.base.harness.state.items.items()
            if found.kind == kind
        )


def make_s7_rig() -> S7Rig:
    base = make_indexing_rig(max_tokens=1000)
    ledger = PurgeLedger()
    return S7Rig(
        base=base,
        blobs=FakeBlobs(base.harness.blobs),
        references=FakeReferences(base.harness.state, ledger),
        ledger=ledger,
    )
