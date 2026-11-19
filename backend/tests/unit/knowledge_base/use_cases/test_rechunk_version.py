"""Rechunking d'une version pour un index d'une autre ``ChunkingPolicy`` (S9 ;
ADR 0009 §7, ADR 0011 §7 ; knowledge-base.md §13, O4).

Contrat de ``RechunkVersionHandler`` (item ``rechunk_version``) :

- ``prepare(command) -> PreparedRechunk | None`` (hors transaction, rien
  d'écrit) : ``None`` sans re-parser si le document est retiré, la version n'est
  plus ``PUBLISHED``/``EXTRACTED`` ou l'index n'est plus vivant ; sinon relit le
  blob de la version et re-parse (O4 : pas de ``StructuredDocument`` persisté),
  segmente et découpe selon la POLITIQUE DE L'INDEX avec le compteur de tokens
  du PROFIL de l'index (``embedders.for_profile(index.profile).token_counter``).
  ``PreparedRechunk`` expose ``document_id``, ``version_id``, ``index_id``,
  ``policy_version`` (identité de la politique de l'index), ``units``, ``chunks``.
- ``apply(transaction, prepared) -> bool`` dans la transaction du work item :
  revérifie (document actif, version indexable, index vivant de cette politique,
  indexation ``PENDING``) ; sinon ``False`` sans aucune écriture. Sinon écrit
  le lot ``(version, politique)`` UNE fois (un lot existant n'est jamais
  réécrit, I7), planifie ``index_version`` (dedup habituelle) et rend ``True``.
  L'état de la version et le lot d'origine ne bougent pas.
- ``fail(transaction, command, reason)`` : l'indexation ``PENDING`` passe
  ``FAILED`` (sinon sans effet), pour que l'échec soit visible et la couverture
  honnête.
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any
from uuid import UUID

import pytest

from knowledge_base.domain.entities.indexation import IndexationStatus
from knowledge_base.domain.entities.knowledge_document import VersionStatus
from knowledge_base.domain.services.point_id import point_id
from knowledge_base.domain.value_objects.failure_reason import FailureReason
from knowledge_base.use_cases.record_extraction.record_extraction_command import (
    RecordExtraction,
)
from tests.unit.knowledge_base.domain.services.support import COUNTER
from tests.unit.knowledge_base.use_cases.fakes import NOW
from tests.unit.knowledge_base.use_cases.s9_support import (
    POLICY_A,
    POLICY_SAME,
    POLICY_SHORT,
    PROFILE_B,
    S9Rig,
    long_sheet,
    make_s9_rig,
    rechunk_command,
)


async def prepared_for_short_policy(
    rig: S9Rig,
) -> tuple[RecordExtraction, UUID, Any]:
    """Version publiée dans l'index actif, migration vers ``POLICY_SHORT``,
    ``rechunk_version`` prêt : (commande, nouvel index, ``PreparedRechunk``)."""
    await rig.provision()
    command = await rig.publish_document(long_sheet("a"))
    new = await rig.start(PROFILE_B, POLICY_SHORT)
    prepared = await rig.rechunk_handler().prepare(rechunk_command(command, new))
    assert prepared is not None
    return command, new, prepared


async def apply(rig: S9Rig, prepared: Any) -> bool:
    async with rig.unit_of_work() as uow:
        applied = await rig.rechunk_handler().apply(uow, prepared)
        await uow.commit()
    return bool(applied)


# --- prepare --------------------------------------------------------------------------


async def test_prepare_rechunks_the_version_under_the_policy_of_the_index() -> None:
    rig = make_s9_rig()
    command, new, prepared = await prepared_for_short_policy(rig)

    assert prepared.policy_version == POLICY_SHORT.identity
    assert (prepared.document_id, prepared.version_id, prepared.index_id) == (
        command.document_id,
        command.version_id,
        new,
    )
    original = rig.chunks_for(command, POLICY_A)
    assert len(original) == 1
    assert len(prepared.chunks) > len(original)
    assert all(c.token_count <= POLICY_SHORT.max_tokens for c in prepared.chunks)


async def test_prepare_reparses_the_original_and_writes_nothing() -> None:
    rig = make_s9_rig()
    await rig.provision()
    command = await rig.publish_document(long_sheet("a"))
    new = await rig.start(PROFILE_B, POLICY_SHORT)
    parsed, commits = rig.parser.calls, rig.store.commits
    batches = list(rig.store.batches)

    await rig.rechunk_handler().prepare(rechunk_command(command, new))

    assert rig.parser.calls == parsed + 1
    assert rig.blob_reader.reads[-1] is not None
    assert rig.store.commits == commits
    assert rig.store.batches == batches
    assert rig.violations == []


async def test_prepare_counts_tokens_with_the_profile_of_the_index() -> None:
    rig = make_s9_rig()
    await rig.provision()
    command = await rig.publish_document(long_sheet("a"))

    class DoubleCounter:
        def count(self, text: str) -> int:
            return 2 * COUNTER.count(text)

    rig.model_b.token_counter = DoubleCounter()  # type: ignore[assignment]
    new = await rig.start(PROFILE_B, POLICY_SHORT)

    prepared = await rig.rechunk_handler().prepare(rechunk_command(command, new))

    assert PROFILE_B in rig.provider.requested
    assert prepared is not None
    for chunk in prepared.chunks:
        assert chunk.token_count == 2 * COUNTER.count(chunk.contextual_text)
        assert chunk.token_count <= POLICY_SHORT.max_tokens


@pytest.mark.parametrize("cause", ["withdrawn", "retired"])
async def test_prepare_has_nothing_to_do_for_a_stale_target(cause: str) -> None:
    rig = make_s9_rig()
    await rig.provision()
    command = await rig.publish_document(long_sheet("a"))
    new = await rig.start(PROFILE_B, POLICY_SHORT)
    if cause == "withdrawn":
        rig.document(command.document_id).withdraw()
    else:
        rig.corpus().retire_index(new, now=NOW)
    parsed = rig.parser.calls

    assert await rig.rechunk_handler().prepare(rechunk_command(command, new)) is None

    assert rig.parser.calls == parsed


# --- apply ----------------------------------------------------------------------------


async def test_apply_adds_the_new_batch_and_keeps_the_original_one() -> None:
    rig = make_s9_rig()
    command, _, prepared = await prepared_for_short_policy(rig)
    original = rig.chunks_for(command, POLICY_A)

    assert await apply(rig, prepared)

    assert set(rig.batches_of(command)) == {POLICY_A.identity, POLICY_SHORT.identity}
    assert rig.chunks_for(command, POLICY_A) == original
    assert rig.chunks_for(command, POLICY_SHORT) == prepared.chunks
    assert {c.chunk_id for c in prepared.chunks}.isdisjoint(
        c.chunk_id for c in original
    )
    assert rig.status(command) is VersionStatus.PUBLISHED


async def test_the_citations_stay_those_of_the_version() -> None:
    rig = make_s9_rig()
    command, _, prepared = await prepared_for_short_policy(rig)
    await apply(rig, prepared)

    def stable(chunks: tuple[Any, ...]) -> set[tuple[Any, ...]]:
        return {
            (
                c.citation.document_id,
                c.citation.document_title,
                c.citation.version_id,
                c.citation.version_number,
                c.citation.source_filename,
            )
            for c in chunks
        }

    assert stable(rig.chunks_for(command, POLICY_SHORT)) == stable(
        rig.chunks_for(command, POLICY_A)
    )


async def test_apply_plans_the_indexation_item_and_keeps_the_indexation_pending() -> (
    None
):
    rig = make_s9_rig()
    command, new, prepared = await prepared_for_short_policy(rig)

    await apply(rig, prepared)

    key = f"index_version:{command.version_id}:{new}"
    assert rig.harness.state.items[key].status == "PENDING"
    assert rig.harness.state.items[key].payload == {
        "document_id": str(command.document_id),
        "version_id": str(command.version_id),
        "index_id": str(new),
    }
    indexation = rig.indexing.indexations[(command.version_id, new)]
    assert indexation.status is IndexationStatus.PENDING
    assert indexation.chunking_policy_version == POLICY_SHORT.identity


async def test_applying_twice_never_rewrites_the_batch() -> None:
    rig = make_s9_rig()
    command, _, prepared = await prepared_for_short_policy(rig)
    await apply(rig, prepared)

    assert await apply(rig, prepared)

    assert (
        len(
            [
                b
                for b in rig.store.batches
                if b.version_id == command.version_id
                and b.policy_version == POLICY_SHORT.identity
            ]
        )
        == 1
    )


@pytest.mark.parametrize("cause", ["withdrawn", "retired", "cancelled"])
async def test_apply_writes_nothing_when_the_target_changed_since_prepare(
    cause: str,
) -> None:
    rig = make_s9_rig()
    command, new, prepared = await prepared_for_short_policy(rig)
    if cause == "withdrawn":
        rig.document(command.document_id).withdraw()
    elif cause == "retired":
        rig.corpus().retire_index(new, now=NOW)
    else:
        rig.indexing.indexations[(command.version_id, new)].cancel()
    batches, items = list(rig.store.batches), dict(rig.harness.state.items)

    assert not await apply(rig, prepared)

    assert rig.store.batches == batches
    assert rig.harness.state.items == items


async def test_a_terminal_failure_marks_the_indexation_failed() -> None:
    rig = make_s9_rig()
    command, new, _ = await prepared_for_short_policy(rig)
    reason = FailureReason(
        code="BLOB_MISSING", message="original absent", retryable=True
    )

    async with rig.unit_of_work() as uow:
        await rig.rechunk_handler().fail(uow, rechunk_command(command, new), reason)
        await uow.commit()

    indexation = rig.indexing.indexations[(command.version_id, new)]
    assert indexation.status is IndexationStatus.FAILED
    assert indexation.failure == reason


# --- de bout en bout : le lot rechunké est celui qui est indexé ---------------


async def test_the_new_index_is_filled_from_the_rechunked_batch() -> None:
    rig = make_s9_rig()
    command, new, prepared = await prepared_for_short_policy(rig)
    await apply(rig, prepared)
    old_points = dict(rig.qdrant.points(rig.active_id()))

    assert await rig.index(command, new)

    expected = {point_id(new, c.chunk_id) for c in prepared.chunks}
    assert {p.point_id for p in rig.points_of(command, new)} == expected
    assert len(expected) > len(rig.chunks_for(command, POLICY_A))
    assert rig.indexing.indexations[(command.version_id, new)].status is (
        IndexationStatus.COMPLETED
    )
    assert dict(rig.qdrant.points(rig.active_id())) == old_points


async def test_an_indexation_without_its_batch_never_completes() -> None:
    rig = make_s9_rig()
    await rig.provision()
    command = await rig.publish_document(long_sheet("a"))
    new = await rig.start(PROFILE_B, POLICY_SHORT)  # rechunk non exécuté : pas de lot

    with suppress(Exception):
        await rig.index(command, new)

    indexation = rig.indexing.indexations[(command.version_id, new)]
    assert indexation.status is not IndexationStatus.COMPLETED
    assert rig.points_of(command, new) == []


async def test_the_original_batch_is_not_used_for_another_policy() -> None:
    rig = make_s9_rig()
    await rig.provision()
    command = await rig.publish_document(long_sheet("a"))
    new = await rig.start(PROFILE_B, POLICY_SAME)  # même politique : lot d'origine

    assert await rig.index(command, new)

    assert {p.point_id for p in rig.points_of(command, new)} == {
        point_id(new, c.chunk_id) for c in rig.chunks_for(command, POLICY_SAME)
    }
