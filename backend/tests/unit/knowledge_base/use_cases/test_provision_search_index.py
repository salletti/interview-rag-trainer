"""``ProvisionSearchIndex`` initial : création et activation du premier index
sur corpus vide (S4 ; knowledge-base.md §5.4, §13 ; ADR 0011 §4).

Choix S4 : commande IDEMPOTENTE, appelée au démarrage de l'application par la
composition (``adapters/controller``) ; aucune route HTTP.

API figée (``knowledge_base.use_cases.provision_search_index``) :

- ``provision_search_index_command.ProvisionSearchIndex(profile:
  EmbeddingProfile, chunking_policy: ChunkingPolicy)``.
- ``provision_search_index_handler.ProvisionSearchIndexHandler(*, unit_of_work,
  vector_index, id_factory, clock)`` ; ``await handle(command) -> UUID``
  (identifiant de l'index actif) :
  1. lit le corpus (``get_for_update``) ; s'il a déjà un index ACTIF, le rend
     sans rien écrire ni appeler Qdrant ;
  2. sinon ``vector_index.ensure_collection(index_id, profile)`` HORS
     transaction, AVANT le commit (un index visible en SQL a toujours sa
     collection ; l'inverse, une collection orpheline, est inoffensif) ;
  3. puis, dans UNE transaction : corpus créé si absent (``add``), index
     provisionné et activé (``coverage_complete=True`` : un corpus sans index
     ne peut contenir aucune version publiée, I9), ``commit``.
  Si ``add`` lève ``CorpusAlreadyExistsError`` (un autre processus a créé le
  corpus entre-temps), l'index actif de ce corpus est rendu.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from knowledge_base.domain.entities.knowledge_corpus import (
    CORPUS_ID,
    IndexStatus,
    KnowledgeCorpus,
)
from tests.unit.knowledge_base.domain.services.support import policy
from tests.unit.knowledge_base.use_cases.fakes import NOW
from tests.unit.knowledge_base.use_cases.indexing_fakes import (
    PROFILE,
    make_indexing_rig,
)

WINNER = UUID(int=777)


async def test_the_first_provisioning_creates_and_activates_the_initial_index() -> None:
    rig = make_indexing_rig()

    index_id = await rig.provision()

    corpus = rig.indexing.corpus
    assert corpus is not None
    assert corpus.corpus_id == CORPUS_ID
    assert corpus.active_index_id == index_id
    active = corpus.active_index()
    assert active is not None
    assert active.status is IndexStatus.ACTIVE
    assert active.profile == PROFILE
    assert active.chunking_policy == policy()
    assert (active.created_at, active.activated_at) == (NOW, NOW)


async def test_the_collection_of_the_index_is_created_with_its_profile() -> None:
    rig = make_indexing_rig()

    index_id = await rig.provision()

    assert index_id in rig.vector.collections
    assert rig.vector.profiles[index_id] == PROFILE


async def test_provisioning_twice_returns_the_same_index_and_creates_nothing() -> None:
    rig = make_indexing_rig()
    first = await rig.provision()
    calls = list(rig.vector.calls)

    second = await rig.provision()

    assert second == first
    assert rig.indexing.corpus is not None
    assert len(rig.indexing.corpus.indexes) == 1
    assert rig.vector.calls == calls


async def test_a_failing_qdrant_leaves_no_index_in_postgresql() -> None:
    rig = make_indexing_rig()
    rig.vector.fail_ensure = True

    with pytest.raises(RuntimeError, match="Qdrant"):
        await rig.provision()

    assert rig.indexing.corpus is None


async def test_a_failing_commit_leaves_no_index_in_postgresql() -> None:
    rig = make_indexing_rig()
    rig.harness.state.fail_commit = True

    with pytest.raises(RuntimeError, match="commit"):
        await rig.provision()

    assert rig.indexing.corpus is None


async def test_the_retry_after_a_failed_commit_succeeds() -> None:
    rig = make_indexing_rig()
    rig.harness.state.fail_commit = True
    with pytest.raises(RuntimeError, match="commit"):
        await rig.provision()
    rig.harness.state.fail_commit = False

    index_id = await rig.provision()

    assert rig.indexing.corpus is not None
    assert rig.indexing.corpus.active_index_id == index_id
    assert index_id in rig.vector.collections


async def test_a_concurrent_creation_of_the_corpus_is_absorbed() -> None:
    rig = make_indexing_rig()
    winner = KnowledgeCorpus.empty()
    winner.provision_index(
        index_id=WINNER,
        profile=PROFILE,
        chunking_policy=policy(),
        now=NOW,
    )
    winner.activate_index(WINNER, now=NOW, coverage_complete=True)
    rig.indexing.race_corpus = winner

    index_id = await rig.provision()

    assert index_id == WINNER
    assert rig.indexing.corpus is not None
    assert len(rig.indexing.corpus.indexes) == 1
