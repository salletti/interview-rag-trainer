"""``CompleteIndexation`` : embeddings, upserts invisibles, vérification du
manifest HORS transaction (``prepare``), puis finalisation (``apply``) (S4 ;
knowledge-base.md §6, §7.3, §8, §9, I8, I9, I14).

API figée (``knowledge_base.use_cases.complete_indexation``) :

- ``complete_indexation_command.CompleteIndexation(document_id, version_id,
  index_id)`` (le payload de l'item ``index_version``).
- ``complete_indexation_handler.CompleteIndexationHandler(*, unit_of_work,
  embedder, vector_index, publisher: PublishDocumentVersionHandler, clock,
  batch_size=32)``.
- ``await prepare(command) -> PreparedIndexation | None`` : lecture seule (ni
  écriture, ni ``commit``), puis, SANS transaction ouverte : vérification de la
  collection (une fois), embeddings par lots de ``batch_size`` sur
  ``contextual_text``, upserts par lots avec ``wait=True`` de points
  ``visible=False`` (payload §9), relecture par identifiants, égalité exacte du
  manifest. ``None`` (aucun travail, aucun appel externe) si le document ou la
  version est inconnu, le document retiré, la version ni ``EXTRACTED`` ni
  ``PUBLISHED``, l'indexation absente ou non ``PENDING``, l'index ``RETIRED``.
  ``PreparedIndexation`` expose ``document_id``, ``version_id``, ``index_id``,
  ``manifest: IndexManifest`` et ``point_count``.
- ``errors.IndexManifestMismatchError`` (point manquant ou ``chunk_hash``
  différent : TRANSITOIRE, la reprise réécrit) ;
  ``errors.CollectionMismatchError`` (collection absente ou non conforme,
  dimension des vecteurs différente du profil : TERMINALE, code
  ``errors.COLLECTION_MISMATCH = "COLLECTION_MISMATCH"``).
- ``await apply(transaction, prepared) -> bool`` : DANS la transaction de
  finalisation, relit document, version, index et indexation ; si tout est
  encore valable : ``Indexation.complete`` (digest du manifest, nombre de
  points, ``clock()``) + ``save``, puis TENTATIVE de publication (refus
  métier avalés : l'indexation reste ``COMPLETED``) ; vrai. Sinon : si la
  version est ``DISCARDED``, le document retiré ou l'index ``RETIRED``,
  l'indexation passe ``CANCELLED`` ; faux. Ne fait jamais de ``commit``.
- ``await fail(transaction, command, reason: FailureReason)`` (``FailIndexation``) :
  ``PENDING`` -> ``FAILED`` ; sans effet ni erreur sinon.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from knowledge_base.domain.entities.indexation import Indexation, IndexationStatus
from knowledge_base.domain.entities.knowledge_document import VersionStatus
from knowledge_base.domain.services.point_id import point_id
from knowledge_base.domain.value_objects.failure_reason import FailureReason
from knowledge_base.domain.value_objects.index_manifest import IndexManifest
from knowledge_base.use_cases.complete_indexation.complete_indexation_command import (
    CompleteIndexation,
)
from knowledge_base.use_cases.complete_indexation.errors import (
    COLLECTION_MISMATCH,
    CollectionMismatchError,
    IndexManifestMismatchError,
)
from knowledge_base.use_cases.record_extraction.record_extraction_command import (
    RecordExtraction,
)
from tests.unit.knowledge_base.use_cases.fakes import NOW
from tests.unit.knowledge_base.use_cases.indexing_fakes import (
    INDEXED_AT,
    PROFILE,
    IndexingRig,
    WrongDimensionEmbedder,
    corrupt_first_hash,
    drop_first,
    make_indexing_rig,
    qa_sheet,
)

BUILDING = UUID(int=555)


async def ready(rig: IndexingRig, questions: int = 2) -> tuple[RecordExtraction, UUID]:
    """Index actif provisionné, version importée et ``EXTRACTED``."""
    index_id = await rig.provision()
    command = await rig.import_document(qa_sheet(questions))
    await rig.extract(command)
    return command, index_id


def request(
    rig: IndexingRig, command: RecordExtraction, index_id: UUID
) -> CompleteIndexation:
    return rig.complete_command(command, index_id)


# --- prepare : effets externes --------------------------------------------------------


async def test_prepare_upserts_one_invisible_point_per_chunk_with_the_payload() -> None:
    rig = make_indexing_rig()
    command, index_id = await ready(rig, questions=3)

    await rig.complete.prepare(request(rig, command, index_id))

    chunks = rig.chunks_of(command)
    points = {p.point_id: p for p in rig.vector.points(index_id).values()}
    assert len(chunks) == 3
    assert set(points) == rig.expected_points(command, index_id)
    for chunk in chunks:
        point = points[point_id(index_id, chunk.chunk_id)]
        assert point.visible is False
        assert (point.chunk_id, point.unit_id) == (chunk.chunk_id, chunk.unit_id)
        assert point.chunk_hash == chunk.chunk_hash
        assert (point.version_id, point.document_id, point.index_id) == (
            command.version_id,
            command.document_id,
            index_id,
        )
        assert (point.topics, point.kind, point.language) == (
            ("python",),
            "QA_SHEET",
            "fr",
        )


async def test_the_vector_of_a_point_embeds_the_contextual_text_of_its_chunk() -> None:
    rig = make_indexing_rig()
    command, index_id = await ready(rig)

    await rig.complete.prepare(request(rig, command, index_id))

    for chunk in rig.chunks_of(command):
        point = rig.vector.points(index_id)[point_id(index_id, chunk.chunk_id)]
        assert point.vector == tuple(rig.embedder.passage_vector(chunk.contextual_text))


async def test_prepare_embeds_and_upserts_by_batches_waiting_for_each_one() -> None:
    rig = make_indexing_rig(batch_size=2)
    command, index_id = await ready(rig, questions=5)

    await rig.complete.prepare(request(rig, command, index_id))

    assert [len(batch) for batch in rig.embedder.passage_calls] == [2, 2, 1]
    assert rig.vector.upsert_sizes == [2, 2, 1]
    assert rig.vector.upsert_waits == [True, True, True]
    assert len(rig.vector.points(index_id)) == 5


async def test_prepare_returns_the_manifest_of_the_chunks_it_wrote() -> None:
    rig = make_indexing_rig()
    command, index_id = await ready(rig, questions=3)

    prepared = await rig.complete.prepare(request(rig, command, index_id))

    assert prepared is not None
    assert prepared.manifest == IndexManifest.for_chunks(
        index_id, rig.chunks_of(command)
    )
    assert prepared.point_count == 3
    assert (prepared.document_id, prepared.version_id, prepared.index_id) == (
        command.document_id,
        command.version_id,
        index_id,
    )


async def test_prepare_verifies_the_collection_once_before_any_embedding() -> None:
    rig = make_indexing_rig(batch_size=1)
    command, index_id = await ready(rig, questions=3)

    await rig.complete.prepare(request(rig, command, index_id))

    assert rig.vector.calls.count("check_collection") == 1
    assert rig.vector.calls.index("check_collection") < rig.vector.calls.index("upsert")


async def test_prepare_reads_the_points_back_after_writing_them() -> None:
    rig = make_indexing_rig()
    command, index_id = await ready(rig)

    await rig.complete.prepare(request(rig, command, index_id))

    assert rig.vector.calls.index("upsert") < rig.vector.calls.index("retrieve")


async def test_prepare_never_calls_the_model_or_qdrant_inside_a_transaction() -> None:
    rig = make_indexing_rig()
    command, index_id = await ready(rig)

    await rig.complete.prepare(request(rig, command, index_id))

    assert rig.vector.upsert_sizes  # le travail a bien eu lieu
    assert rig.violations == []


async def test_prepare_writes_nothing_in_postgresql() -> None:
    rig = make_indexing_rig()
    command, index_id = await ready(rig)
    commits = rig.store.commits

    await rig.complete.prepare(request(rig, command, index_id))

    assert rig.store.commits == commits
    assert rig.indexation(command, index_id).status is IndexationStatus.PENDING
    assert rig.status(command) is VersionStatus.EXTRACTED


async def test_replaying_prepare_gives_same_points_and_manifest_no_duplicates() -> None:
    rig = make_indexing_rig()
    command, index_id = await ready(rig, questions=3)

    first = await rig.complete.prepare(request(rig, command, index_id))
    snapshot = dict(rig.vector.points(index_id))
    second = await rig.complete.prepare(request(rig, command, index_id))

    assert first is not None
    assert first == second
    assert rig.vector.points(index_id) == snapshot
    assert len(rig.vector.points(index_id)) == 3


# --- prepare : vérification du manifest (I8) ------------------------------------------


async def test_a_missing_point_prevents_the_verification() -> None:
    rig = make_indexing_rig()
    command, index_id = await ready(rig, questions=3)
    rig.vector.after_upsert = drop_first

    with pytest.raises(IndexManifestMismatchError):
        await rig.complete.prepare(request(rig, command, index_id))

    assert rig.indexation(command, index_id).status is IndexationStatus.PENDING


async def test_a_modified_chunk_hash_prevents_the_verification() -> None:
    rig = make_indexing_rig()
    command, index_id = await ready(rig, questions=3)
    rig.vector.after_upsert = corrupt_first_hash

    with pytest.raises(IndexManifestMismatchError):
        await rig.complete.prepare(request(rig, command, index_id))

    assert rig.indexation(command, index_id).status is IndexationStatus.PENDING


async def test_a_manifest_divergence_is_transient_the_retry_rewrites_the_points() -> (
    None
):
    rig = make_indexing_rig()
    command, index_id = await ready(rig, questions=3)
    rig.vector.after_upsert = drop_first
    with pytest.raises(IndexManifestMismatchError):
        await rig.complete.prepare(request(rig, command, index_id))
    rig.vector.after_upsert = None

    prepared = await rig.complete.prepare(request(rig, command, index_id))

    assert prepared is not None
    assert set(rig.vector.points(index_id)) == rig.expected_points(command, index_id)


async def test_a_nonconforming_collection_is_terminal_and_nothing_is_embedded() -> None:
    rig = make_indexing_rig()
    command, index_id = await ready(rig)
    rig.vector.conforming = False

    with pytest.raises(CollectionMismatchError) as raised:
        await rig.complete.prepare(request(rig, command, index_id))

    assert raised.value.code == COLLECTION_MISMATCH == "COLLECTION_MISMATCH"
    assert rig.embedder.passage_calls == []
    assert rig.vector.upsert_sizes == []


async def test_vectors_of_the_wrong_dimension_are_terminal_and_never_written() -> None:
    rig = make_indexing_rig(embedder=WrongDimensionEmbedder(PROFILE))
    command, index_id = await ready(rig)

    with pytest.raises(CollectionMismatchError):
        await rig.complete.prepare(request(rig, command, index_id))

    assert rig.vector.points(index_id) == {}


# --- prepare : rien à faire -----------------------------------------------------------


async def test_prepare_does_nothing_for_a_discarded_version() -> None:
    rig = make_indexing_rig()
    command, index_id = await ready(rig)
    await rig.import_next_version(command.document_id, qa_sheet(1, tag="v2-"))

    assert await rig.complete.prepare(request(rig, command, index_id)) is None
    assert rig.status(command) is VersionStatus.DISCARDED
    assert rig.vector.calls == ["ensure_collection"]
    assert rig.embedder.passage_calls == []


async def test_prepare_does_nothing_for_a_withdrawn_document() -> None:
    rig = make_indexing_rig()
    command, index_id = await ready(rig)
    rig.document(command.document_id).withdraw()

    assert await rig.complete.prepare(request(rig, command, index_id)) is None
    assert rig.embedder.passage_calls == []


async def test_prepare_does_nothing_without_an_indexation() -> None:
    rig = make_indexing_rig()
    command = await rig.import_document(qa_sheet())
    await rig.extract(command)  # aucun index : aucune indexation
    assert rig.indexing.indexations == {}

    assert await rig.complete.prepare(request(rig, command, UUID(int=1))) is None


async def test_prepare_does_nothing_for_an_already_completed_indexation() -> None:
    rig = make_indexing_rig()
    command, index_id = await ready(rig)
    assert await rig.index(command, index_id)
    rig.vector.calls.clear()

    assert await rig.complete.prepare(request(rig, command, index_id)) is None
    assert rig.vector.calls == []


async def test_prepare_does_nothing_for_a_retired_index() -> None:
    rig = make_indexing_rig()
    await rig.provision()
    rig.add_building_index(BUILDING)
    command = await rig.import_document(qa_sheet())
    await rig.extract(command)
    assert rig.indexing.corpus is not None
    rig.indexing.corpus.retire_index(BUILDING, now=NOW)

    assert await rig.complete.prepare(request(rig, command, BUILDING)) is None
    assert rig.vector.points(BUILDING) == {}


# --- apply : finalisation -------------------------------------------------------------


async def test_apply_completes_the_indexation_with_the_manifest_digest() -> None:
    rig = make_indexing_rig()
    command, index_id = await ready(rig, questions=3)

    assert await rig.index(command, index_id) is True

    indexation = rig.indexation(command, index_id)
    expected = IndexManifest.for_chunks(index_id, rig.chunks_of(command))
    assert indexation.status is IndexationStatus.COMPLETED
    assert indexation.manifest_digest == expected.digest
    assert indexation.point_count == 3
    assert indexation.completed_at == INDEXED_AT


async def test_apply_publishes_the_version_once_indexed_in_the_active_index() -> None:
    rig = make_indexing_rig()
    command, index_id = await ready(rig)

    await rig.index(command, index_id)

    document = rig.document(command.document_id)
    assert rig.status(command) is VersionStatus.PUBLISHED
    assert document.published_version_id == command.version_id
    assert rig.sync_keys() == [f"sync_visibility:{command.document_id}:1"]


async def test_published_points_stay_invisible_until_the_synchronization() -> None:
    rig = make_indexing_rig()
    command, index_id = await ready(rig)

    await rig.index(command, index_id)

    assert rig.points_of(command, index_id)
    assert all(not p.visible for p in rig.points_of(command, index_id))


async def test_completion_in_a_building_index_alone_does_not_publish_the_version() -> (
    None
):
    rig = make_indexing_rig()
    active = await rig.provision()
    rig.add_building_index(BUILDING)
    command = await rig.import_document(qa_sheet())
    await rig.extract(command)

    await rig.index(command, BUILDING)

    assert rig.indexation(command, BUILDING).status is IndexationStatus.COMPLETED
    assert rig.indexation(command, active).status is IndexationStatus.PENDING
    assert rig.status(command) is VersionStatus.EXTRACTED
    assert (
        rig.sync_keys() == []
    )  # la tentative de publication a été refusée, sans erreur

    await rig.index(command, active)

    assert rig.status(command) is VersionStatus.PUBLISHED


async def test_apply_cancels_the_indexation_of_a_version_discarded_meanwhile() -> None:
    rig = make_indexing_rig()
    command, index_id = await ready(rig)
    prepared = await rig.complete.prepare(request(rig, command, index_id))
    assert prepared is not None
    await rig.import_next_version(command.document_id, qa_sheet(1, tag="v2-"))

    async with rig.unit_of_work() as uow:
        applied = await rig.complete.apply(uow, prepared)
        await uow.commit()

    assert applied is False
    assert rig.indexation(command, index_id).status is IndexationStatus.CANCELLED
    assert rig.sync_keys() == []


async def test_apply_cancels_the_indexation_of_a_document_withdrawn_meanwhile() -> None:
    rig = make_indexing_rig()
    command, index_id = await ready(rig)
    prepared = await rig.complete.prepare(request(rig, command, index_id))
    assert prepared is not None
    rig.document(command.document_id).withdraw()

    async with rig.unit_of_work() as uow:
        applied = await rig.complete.apply(uow, prepared)
        await uow.commit()

    assert applied is False
    assert rig.indexation(command, index_id).status is IndexationStatus.CANCELLED
    assert rig.status(command) is VersionStatus.EXTRACTED


async def test_apply_cancels_the_indexation_of_an_index_retired_meanwhile() -> None:
    rig = make_indexing_rig()
    await rig.provision()
    rig.add_building_index(BUILDING)
    command = await rig.import_document(qa_sheet())
    await rig.extract(command)
    prepared = await rig.complete.prepare(request(rig, command, BUILDING))
    assert prepared is not None
    assert rig.indexing.corpus is not None
    rig.indexing.corpus.retire_index(BUILDING, now=NOW)

    async with rig.unit_of_work() as uow:
        applied = await rig.complete.apply(uow, prepared)
        await uow.commit()

    assert applied is False
    assert rig.indexation(command, BUILDING).status is IndexationStatus.CANCELLED


async def test_apply_never_commits_the_transaction_it_is_given() -> None:
    rig = make_indexing_rig()
    command, index_id = await ready(rig)

    assert await rig.index(command, index_id, commit=False) is True

    assert rig.indexation(command, index_id).status is IndexationStatus.PENDING
    assert rig.status(command) is VersionStatus.EXTRACTED
    assert rig.sync_keys() == []


async def test_apply_on_a_published_version_completes_without_republishing() -> None:
    rig = make_indexing_rig()
    command, index_id = await ready(rig)
    await rig.index(command, index_id)
    revision = rig.document(command.document_id).visibility_revision
    rig.add_building_index(BUILDING)
    rig.indexing.indexations[(command.version_id, BUILDING)] = Indexation.pending(
        version_id=command.version_id,
        index_id=BUILDING,
        chunking_policy_version=rig.indexation(
            command, index_id
        ).chunking_policy_version,
    )

    assert await rig.index(command, BUILDING) is True

    assert rig.indexation(command, BUILDING).status is IndexationStatus.COMPLETED
    assert rig.document(command.document_id).visibility_revision == revision
    assert rig.sync_keys() == [f"sync_visibility:{command.document_id}:1"]


# --- fail -----------------------------------------------------------------------------


async def test_fail_moves_a_pending_indexation_to_failed_with_the_reason() -> None:
    rig = make_indexing_rig()
    command, index_id = await ready(rig)
    reason = FailureReason(code="COLLECTION_MISMATCH", message="dim 17", retryable=True)

    async with rig.unit_of_work() as uow:
        await rig.complete.fail(uow, request(rig, command, index_id), reason)
        await uow.commit()

    indexation = rig.indexation(command, index_id)
    assert indexation.status is IndexationStatus.FAILED
    assert indexation.failure == reason


@pytest.mark.parametrize("final", ["completed", "cancelled"])
async def test_fail_leaves_a_settled_indexation_untouched(final: str) -> None:
    rig = make_indexing_rig()
    command, index_id = await ready(rig)
    if final == "completed":
        await rig.index(command, index_id)
    else:
        rig.indexation(command, index_id).cancel()
    expected = rig.indexation(command, index_id).status
    reason = FailureReason(code="X", message="y", retryable=True)

    async with rig.unit_of_work() as uow:
        await rig.complete.fail(uow, request(rig, command, index_id), reason)
        await uow.commit()

    assert rig.indexation(command, index_id).status is expected
    assert rig.indexation(command, index_id).failure is None


async def test_fail_without_an_indexation_is_a_silent_no_op() -> None:
    rig = make_indexing_rig()
    command = await rig.import_document(qa_sheet())
    reason = FailureReason(code="X", message="y", retryable=True)

    async with rig.unit_of_work() as uow:
        await rig.complete.fail(uow, request(rig, command, UUID(int=1)), reason)
        await uow.commit()

    assert rig.indexing.indexations == {}
