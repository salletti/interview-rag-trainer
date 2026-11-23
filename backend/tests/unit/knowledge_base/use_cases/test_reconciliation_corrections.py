"""Corrections de la réconciliation, revue S8 (M2, M3, M8), sans base.

- M2 : une transaction par document replanifié, qui RELIT le document avant
  d'écrire (un constat périmé n'écrit rien) ; l'échec d'un document n'annule
  ni n'empêche les autres, et le cycle échoue quand même (rien n'est
  « réussi » en silence).
- M3 : la borne des réouvertures automatiques d'une purge est relue sous le
  verrou de l'item, pas prise dans le constat.
- M8 : ``COMPLETED_NOT_PUBLISHED`` ne compte que les publications rattrapées ;
  un refus (même persistant) n'en est pas un, sinon le rapport ne serait
  jamais propre.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import TracebackType
from typing import Any, Self, cast
from uuid import UUID, uuid4

import pytest

from knowledge_base.domain.entities.indexation import IndexationStatus
from knowledge_base.domain.entities.knowledge_corpus import IndexStatus, SearchIndex
from knowledge_base.domain.entities.knowledge_document import (
    DocumentStatus,
    VersionStatus,
)
from knowledge_base.domain.providers.failed_work import (
    WorkItemRecord,
    WorkItemStatus,
)
from knowledge_base.domain.providers.point_inventory import ReconcilableIndex
from knowledge_base.domain.providers.reconciliation_store import (
    FailedItemFacts,
    PublicationCandidate,
    ReconciliationStore,
    ReconciliationUnitOfWork,
)
from knowledge_base.domain.services.reconciliation import (
    DiscrepancyKind,
    DocumentFacts,
)
from knowledge_base.domain.value_objects.point_record import PointRecord
from knowledge_base.use_cases.publish_document_version.errors import (
    PublicationRefusedError,
)
from knowledge_base.use_cases.publish_document_version.publish_document_version_command import (  # noqa: E501
    PublishDocumentVersion,
)
from knowledge_base.use_cases.publish_document_version.publish_document_version_handler import (  # noqa: E501
    PublishDocumentVersionHandler,
)
from knowledge_base.use_cases.reconcile_knowledge_base.failed_work_reconciliation import (  # noqa: E501
    FailedWorkReconciliation,
)
from knowledge_base.use_cases.reconcile_knowledge_base.indexation_reconciliation import (  # noqa: E501
    IndexationReconciliation,
)
from knowledge_base.use_cases.reconcile_knowledge_base.point_reconciliation import (
    PointReconciliation,
)
from tests.unit.knowledge_base.use_cases.indexing_fakes import (
    IndexingRig,
    IndexingUnitOfWork,
    make_indexing_rig,
    qa_sheet,
)

# --- M2 : points invisibles de versions publiées --------------------------------------


class ScannableIndex:
    """Le faux index de S4 plus le parcours de ``PointInventory``."""

    def __init__(self, rig: IndexingRig) -> None:
        self._vector = rig.vector

    def scan_points(
        self, index_id: UUID, *, batch_size: int = 256
    ) -> AsyncIterator[PointRecord]:
        async def scan() -> AsyncIterator[PointRecord]:
            for point in list(self._vector.points(index_id).values()):
                yield PointRecord(
                    point_id=point.point_id,
                    document_id=point.document_id,
                    version_id=point.version_id,
                    chunk_hash=point.chunk_hash,
                    visible=point.visible,
                )

        return scan()

    async def delete_points(self, index_id: UUID, selector: Any) -> None:
        await self._vector.delete_points(index_id, selector)


@dataclass
class SnapshotStore:
    """Constat figé au moment de sa construction : ce que le cycle a lu
    AVANT que l'état ne change."""

    facts: Mapping[UUID, DocumentFacts]

    async def documents(
        self, document_ids: Collection[UUID]
    ) -> Mapping[UUID, DocumentFacts]:
        return {i: f for i, f in self.facts.items() if i in document_ids}

    async def documents_with_live_work(self, kind: str) -> frozenset[UUID]:
        return frozenset()


def facts_of(rig: IndexingRig, document_id: UUID) -> DocumentFacts:
    document = rig.document(document_id)
    return DocumentFacts(
        status=document.status,
        purged=False,
        versions={v.version_id: v.status for v in document.versions},
    )


async def published_and_hidden(rig: IndexingRig, index_id: UUID, tag: str) -> UUID:
    """Document publié et synchronisé, dont les points sont redevenus
    invisibles (tentative zombie) : la réconciliation doit resynchroniser."""
    command = await rig.import_document(qa_sheet(2, tag=tag), title=f"Fiche {tag}")
    await rig.extract(command)
    assert await rig.index(command, index_id) is True
    for item in rig.harness.state.items.values():
        if item.kind == "sync_visibility":
            item.status = "SUCCEEDED"
    return command.document_id


def sync_items(rig: IndexingRig, document_id: UUID) -> list[str]:
    return sorted(
        key
        for key, item in rig.harness.state.items.items()
        if item.kind == "sync_visibility"
        and item.payload["document_id"] == str(document_id)
        and item.status == "PENDING"
    )


def points_reconciliation(
    rig: IndexingRig,
    store: SnapshotStore,
    unit_of_work: Callable[[], IndexingUnitOfWork] | None = None,
) -> PointReconciliation:
    factory = unit_of_work or rig.unit_of_work
    return PointReconciliation(
        unit_of_work=lambda: cast(ReconciliationUnitOfWork, factory()),
        store=cast(ReconciliationStore, store),
        vector_index=cast(ReconcilableIndex, ScannableIndex(rig)),
        scan_batch_size=256,
    )


def live_index(rig: IndexingRig) -> SearchIndex:
    corpus = rig.indexing.corpus
    assert corpus is not None
    (index,) = corpus.live_indexes()
    return index


async def test_a_document_withdrawn_since_the_scan_gets_no_synchronization() -> None:
    rig = make_indexing_rig()
    index_id = await rig.provision()
    stale = await published_and_hidden(rig, index_id, "a")
    kept = await published_and_hidden(rig, index_id, "b")
    store = SnapshotStore({d: facts_of(rig, d) for d in (stale, kept)})
    rig.document(stale).withdraw()  # après le constat, avant la correction

    await points_reconciliation(rig, store).run([live_index(rig)])

    assert sync_items(rig, stale) == []
    assert sync_items(rig, kept) == [f"sync_visibility:{kept}:1:2"]


class FailingDocuments:
    """Lecture d'un document qui échoue (panne SQL sur cette ligne)."""

    def __init__(self, inner: Any, failing: UUID) -> None:
        self._inner = inner
        self._failing = failing

    async def get(self, document_id: UUID) -> Any:
        if document_id == self._failing:
            raise RuntimeError(f"lecture du document {document_id} impossible")
        return await self._inner.get(document_id)


async def test_a_failing_document_neither_cancels_nor_blocks_the_others() -> None:
    rig = make_indexing_rig()
    index_id = await rig.provision()
    documents = sorted(
        [await published_and_hidden(rig, index_id, tag) for tag in "abc"]
    )
    failing = documents[1]  # au milieu : un avant, un après
    store = SnapshotStore({d: facts_of(rig, d) for d in documents})

    def unit_of_work() -> IndexingUnitOfWork:
        uow = rig.unit_of_work()
        uow.documents = FailingDocuments(uow.documents, failing)  # type: ignore[assignment]
        return uow

    with pytest.raises(ExceptionGroup) as raised:
        await points_reconciliation(rig, store, unit_of_work).run([live_index(rig)])

    assert len(raised.value.exceptions) == 1
    assert sync_items(rig, failing) == []
    for document_id in (documents[0], documents[2]):
        assert sync_items(rig, document_id) == [f"sync_visibility:{document_id}:1:2"]


# --- M3 : borne de réouverture relue sous verrou --------------------------------------


def failed_purge(reopen_count: int) -> WorkItemRecord:
    return WorkItemRecord(
        item_id=UUID(int=1),
        kind="purge_document",
        status=WorkItemStatus.FAILED,
        payload={"document_id": str(UUID(int=2))},
        attempts=5,
        max_attempts=5,
        last_error_code="TRANSIENT",
        last_error_message="Qdrant injoignable",
        failed_at=None,
        reopen_count=reopen_count,
    )


@dataclass
class LockedItems:
    current: WorkItemRecord
    reopened: list[UUID] = field(default_factory=list)

    async def get_for_update(self, item_id: UUID) -> WorkItemRecord | None:
        return self.current

    async def reopen(self, item_id: UUID) -> bool:
        self.reopened.append(item_id)
        return True


class ItemsUnitOfWork:
    def __init__(self, items: LockedItems) -> None:
        self.failed_work = items
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


@dataclass
class FailedItemsStore:
    facts: Sequence[FailedItemFacts]

    async def failed_items(self, kinds: Collection[str]) -> Sequence[FailedItemFacts]:
        return self.facts


@pytest.mark.parametrize(("current_count", "reopened"), [(2, True), (3, False)])
async def test_the_purge_reopening_bound_is_read_under_the_lock(
    current_count: int, reopened: bool
) -> None:
    seen = failed_purge(reopen_count=0)  # constat : aucune réouverture
    items = LockedItems(current=replace(seen, reopen_count=current_count))
    store = FailedItemsStore(
        [
            FailedItemFacts(
                item=seen,
                document_status=DocumentStatus.WITHDRAWN,
                purged=False,
                version_status=None,
                indexation_status=None,
                live_work=False,
            )
        ]
    )
    reconciliation = FailedWorkReconciliation(
        unit_of_work=lambda: cast(ReconciliationUnitOfWork, ItemsUnitOfWork(items)),
        store=cast(ReconciliationStore, store),
        max_purge_reopenings=3,
    )

    counts = await reconciliation.run()

    assert (items.reopened == [seen.item_id]) is reopened
    assert counts[DiscrepancyKind.FAILED_PURGE] == (1 if reopened else 0)


# --- M8 : publications perdues --------------------------------------------------------


def candidate() -> PublicationCandidate:
    return PublicationCandidate(
        document_id=uuid4(),
        version_id=uuid4(),
        index_id=uuid4(),
        document_status=DocumentStatus.ACTIVE,
        version_status=VersionStatus.EXTRACTED,
        version_number=2,
        published_number=1,
        indexation_status=IndexationStatus.COMPLETED,
        index_status=IndexStatus.ACTIVE,
    )


@dataclass
class CandidatesStore:
    candidates: Sequence[PublicationCandidate]

    async def publication_candidates(self) -> Sequence[PublicationCandidate]:
        return self.candidates


class StubPublisher:
    def __init__(self, *, refuse: bool) -> None:
        self._refuse = refuse
        self.commands: list[PublishDocumentVersion] = []

    async def apply(self, transaction: Any, command: PublishDocumentVersion) -> None:
        self.commands.append(command)
        if self._refuse:
            raise PublicationRefusedError(command.version_id, "refus persistant")


class EmptyUnitOfWork(ItemsUnitOfWork):
    def __init__(self) -> None:
        super().__init__(LockedItems(failed_purge(0)))


def indexation_reconciliation(
    store: CandidatesStore, publisher: StubPublisher
) -> IndexationReconciliation:
    return IndexationReconciliation(
        unit_of_work=lambda: cast(ReconciliationUnitOfWork, EmptyUnitOfWork()),
        store=cast(ReconciliationStore, store),
        vector_index=cast(ReconcilableIndex, object()),
        publisher=cast(PublishDocumentVersionHandler, publisher),
        sampler=lambda candidates, k: list(candidates)[:k],
    )


async def test_a_lost_publication_caught_up_is_counted() -> None:
    publisher = StubPublisher(refuse=False)
    reconciliation = indexation_reconciliation(
        CandidatesStore([candidate()]), publisher
    )

    counts = await reconciliation.publish_lost_publications()

    assert counts[DiscrepancyKind.COMPLETED_NOT_PUBLISHED] == 1
    assert len(publisher.commands) == 1


async def test_a_refused_publication_is_never_counted_so_the_report_can_be_clean() -> (
    None
):
    publisher = StubPublisher(refuse=True)
    reconciliation = indexation_reconciliation(
        CandidatesStore([candidate()]), publisher
    )

    first = await reconciliation.publish_lost_publications()
    second = await reconciliation.publish_lost_publications()

    assert len(publisher.commands) == 2  # tentée à chaque cycle
    assert first[DiscrepancyKind.COMPLETED_NOT_PUBLISHED] == 0
    assert second[DiscrepancyKind.COMPLETED_NOT_PUBLISHED] == 0
