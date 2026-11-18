"""Manifest d'une indexation et identifiant de point (S4 ; knowledge-base.md
§7.3 et §9 ; ADR 0010 §2 et §3).

API figée :

- ``knowledge_base.domain.services.point_id.point_id(index_id, chunk_id) ->
  UUID`` = ``uuid5(index_id, str(chunk_id))`` (``chunk_id`` en texte canonique).
- ``knowledge_base.domain.value_objects.index_manifest.IndexManifest`` (dataclass
  figée) :
  - ``IndexManifest.of(entries: Iterable[tuple[UUID, str]])`` : couples
    ``(point_id, chunk_hash)`` ; ``entries`` est un ``tuple`` TRIÉ par
    ``str(point_id)`` ; un ``point_id`` en double lève ``ValueError``.
  - ``IndexManifest.for_chunks(index_id, chunks: Iterable[DocumentChunk])``.
  - ``digest`` : SHA-256 hexadécimal de la sérialisation canonique : pour
    chaque couple trié, la ligne ``"<point_id>:<chunk_hash>\\n"``, concaténées,
    encodées en UTF-8. Le manifest vide a le digest de la chaîne vide.
  - ``len(manifest)`` est le nombre de points.
"""

from __future__ import annotations

import hashlib
from uuid import UUID, uuid5

import pytest

from knowledge_base.domain.services.point_id import point_id
from knowledge_base.domain.value_objects.index_manifest import IndexManifest
from tests.unit.knowledge_base.domain.services.support import make_unit, p, plan

INDEX_A = UUID(int=0xA)
INDEX_B = UUID(int=0xB)
P1, P2, P3 = UUID(int=1), UUID(int=2), UUID(int=3)

# --- identifiant de point -------------------------------------------------------------


def test_a_point_id_is_the_uuid5_of_the_chunk_in_the_index_namespace() -> None:
    chunk_id = UUID("12345678-1234-5678-1234-567812345678")

    assert point_id(INDEX_A, chunk_id) == uuid5(INDEX_A, str(chunk_id))


def test_a_point_id_is_stable_across_calls() -> None:
    chunk_id = UUID(int=42)

    assert point_id(INDEX_A, chunk_id) == point_id(INDEX_A, chunk_id)


def test_the_same_chunk_gets_a_different_point_in_another_index() -> None:
    chunk_id = UUID(int=42)

    assert point_id(INDEX_A, chunk_id) != point_id(INDEX_B, chunk_id)


def test_two_chunks_of_the_same_index_get_different_points() -> None:
    assert point_id(INDEX_A, UUID(int=1)) != point_id(INDEX_A, UUID(int=2))


# --- manifest : ordre canonique et digest ---------------------------------------------


def test_entries_are_sorted_by_point_id_whatever_the_input_order() -> None:
    manifest = IndexManifest.of([(P3, "c"), (P1, "a"), (P2, "b")])

    assert manifest.entries == ((P1, "a"), (P2, "b"), (P3, "c"))


def test_the_digest_is_the_sha256_of_the_canonical_serialization() -> None:
    manifest = IndexManifest.of([(P2, "hb"), (P1, "ha")])

    canonical = f"{P1}:ha\n{P2}:hb\n".encode()
    assert manifest.digest == hashlib.sha256(canonical).hexdigest()


def test_the_digest_does_not_depend_on_the_input_order() -> None:
    forward = IndexManifest.of([(P1, "a"), (P2, "b"), (P3, "c")])
    shuffled = IndexManifest.of([(P3, "c"), (P1, "a"), (P2, "b")])

    assert forward.digest == shuffled.digest
    assert forward == shuffled


def test_the_digest_is_stable_across_instances() -> None:
    entries = [(P1, "a"), (P2, "b")]

    assert IndexManifest.of(entries).digest == IndexManifest.of(entries).digest


def test_the_digest_changes_when_one_chunk_hash_changes() -> None:
    before = IndexManifest.of([(P1, "a"), (P2, "b")])
    after = IndexManifest.of([(P1, "a"), (P2, "B")])

    assert before.digest != after.digest
    assert before != after


def test_the_digest_changes_when_a_point_is_missing() -> None:
    full = IndexManifest.of([(P1, "a"), (P2, "b")])
    partial = IndexManifest.of([(P1, "a")])

    assert full.digest != partial.digest


def test_the_digest_changes_when_a_hash_moves_to_another_point() -> None:
    before = IndexManifest.of([(P1, "a"), (P2, "b")])
    swapped = IndexManifest.of([(P1, "b"), (P2, "a")])

    assert before.digest != swapped.digest


def test_the_empty_manifest_has_the_digest_of_the_empty_string() -> None:
    manifest = IndexManifest.of([])

    assert manifest.entries == ()
    assert len(manifest) == 0
    assert manifest.digest == hashlib.sha256(b"").hexdigest()


def test_the_length_is_the_number_of_points() -> None:
    assert len(IndexManifest.of([(P1, "a"), (P2, "b"), (P3, "c")])) == 3


def test_a_duplicated_point_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="point"):
        IndexManifest.of([(P1, "a"), (P1, "a")])


# --- depuis les chunks ----------------------------------------------------------------


def test_the_manifest_of_chunks_pairs_each_derived_point_with_its_hash() -> None:
    unit = make_unit(p("Premier paragraphe."), ordinal=0)
    chunks = plan(unit)
    assert chunks  # garde-fou : le test ne doit pas porter sur un manifest vide

    manifest = IndexManifest.for_chunks(INDEX_A, chunks)

    assert manifest == IndexManifest.of(
        [(point_id(INDEX_A, c.chunk_id), c.chunk_hash) for c in chunks]
    )
    assert len(manifest) == len(chunks)


def test_the_manifest_of_chunks_differs_between_two_indexes() -> None:
    chunks = plan(make_unit(p("Texte."), ordinal=0))

    a = IndexManifest.for_chunks(INDEX_A, chunks)
    b = IndexManifest.for_chunks(INDEX_B, chunks)

    assert chunks
    assert a.digest != b.digest
    assert {h for _, h in a.entries} == {h for _, h in b.entries}
