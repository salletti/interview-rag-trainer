"""Indexation avec l'``Embedder`` du PROFIL de l'index (S9 ; reports S4 M3 :
« un seul Embedder par worker : un index d'un autre profil échoue en
CollectionMismatchError terminal » ; ADR 0011 « deux modèles peuvent être
chargés pendant la transition »).

Contrat : ``CompleteIndexationHandler(..., embedders=<EmbedderProvider>)`` (seule
forme depuis la revue S9) choisit ``embedders.for_profile(index.profile)`` pour
chaque indexation ; un profil que le fournisseur ne sait pas charger reste le
refus terminal existant, ``CollectionMismatchError``, avant tout encodage et
toute écriture.
"""

from __future__ import annotations

import pytest

from knowledge_base.domain.entities.indexation import IndexationStatus
from knowledge_base.use_cases.complete_indexation.errors import CollectionMismatchError
from tests.unit.knowledge_base.use_cases.indexing_fakes import (
    hash_vector,
    qa_sheet,
)
from tests.unit.knowledge_base.use_cases.s9_support import PROFILE_B, make_s9_rig


async def test_a_building_index_of_another_profile_is_filled_by_its_own_model() -> None:
    rig = make_s9_rig()
    await rig.provision()
    command = await rig.publish_document(qa_sheet(2, tag="a"))
    new = await rig.start(PROFILE_B)
    before_a = len(rig.model_a.passage_calls)

    assert await rig.index(command, new)

    assert len(rig.model_a.passage_calls) == before_a
    assert len(rig.model_b.passage_calls) == 1
    chunks = rig.chunks_of(command)
    assert rig.model_b.passage_calls[0] == [c.contextual_text for c in chunks]
    points = rig.points_of(command, new)
    assert len(points) == len(chunks)
    assert all(len(p.vector) == PROFILE_B.dimension for p in points)
    assert rig.indexing.indexations[(command.version_id, new)].status is (
        IndexationStatus.COMPLETED
    )


async def test_the_vectors_carry_the_prefix_of_the_profile_of_the_index() -> None:
    rig = make_s9_rig()
    await rig.provision()
    command = await rig.publish_document(qa_sheet(2, tag="a"))
    new = await rig.start(PROFILE_B)

    await rig.index(command, new)

    by_chunk = {p.chunk_id: p for p in rig.points_of(command, new)}
    for chunk in rig.chunks_of(command):
        expected = hash_vector(
            PROFILE_B.passage_prefix + chunk.contextual_text, PROFILE_B.dimension
        )
        assert list(by_chunk[chunk.chunk_id].vector) == pytest.approx(expected)


async def test_each_indexation_asks_the_provider_for_the_profile_of_its_index() -> None:
    rig = make_s9_rig()
    await rig.provision()
    command = await rig.publish_document(qa_sheet(2, tag="a"))
    new = await rig.start(PROFILE_B)
    rig.provider.requested.clear()

    await rig.index(command, new)

    assert set(rig.provider.requested) == {PROFILE_B}


async def test_a_profile_the_worker_cannot_load_is_refused_before_any_write() -> None:
    rig = make_s9_rig()
    await rig.provision()
    command = await rig.publish_document(qa_sheet(2, tag="a"))
    new = await rig.start(PROFILE_B)
    del rig.provider.models[PROFILE_B]
    touched = list(rig.qdrant.touched)

    with pytest.raises(CollectionMismatchError):
        await rig.index(command, new)

    assert rig.qdrant.touched == touched
    assert len(rig.model_a.passage_calls) == 1  # la seule publication initiale
    assert rig.model_b.passage_calls == []
    assert rig.indexing.indexations[(command.version_id, new)].status is (
        IndexationStatus.PENDING
    )
