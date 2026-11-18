"""``SqlAlchemyKnowledgeCorpusRepository`` contre PostgreSQL réel (revue S4 ;
knowledge-base.md §3.2, §13 ; ADR 0011 §5).

- Barrière blue/green : ``get_shared`` tient la ligne du corpus en
  ``FOR SHARE`` jusqu'à la fin de la transaction ; un ``FOR UPDATE NOWAIT``
  concurrent (l'activation de S9) échoue, une autre lecture partagée passe.
  ``get_for_update`` exclut aussi les lectures partagées.
- Provisionnement concurrent : deux transactions qui ont toutes deux lu « pas
  de corpus » n'en créent qu'un, avec un seul index ; le perdant reçoit
  ``CorpusAlreadyExistsError``. Même résultat par le handler de production.
- Verrou optimiste de ``save`` : une copie périmée lève
  ``CorpusModifiedConcurrentlyError`` sans rien écrire.

Le verrou est observé par ``NOWAIT`` (erreur ``55P03`` immédiate) plutôt que
par une attente chronométrée : aucun délai dans le test.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from knowledge_base.adapters.controller.composition import (
    build_provision_search_index_handler,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_knowledge_corpus_repository import (  # noqa: E501
    SqlAlchemyKnowledgeCorpusRepository,
)
from knowledge_base.domain.entities.knowledge_corpus import (
    CORPUS_ID,
    IndexStatus,
    KnowledgeCorpus,
)
from knowledge_base.domain.providers.knowledge_corpus_repository import (
    CorpusAlreadyExistsError,
    CorpusModifiedConcurrentlyError,
)
from knowledge_base.use_cases.provision_search_index.provision_search_index_command import (  # noqa: E501
    ProvisionSearchIndex,
)
from runtime.database import SessionFactory
from tests.unit.knowledge_base.domain.services.support import policy
from tests.unit.knowledge_base.use_cases.fakes import NOW
from tests.unit.knowledge_base.use_cases.indexing_fakes import (
    PROFILE,
    FakeVectorIndex,
)

pytestmark = pytest.mark.integration

LOCK_NOT_AVAILABLE = "55P03"
FOR_UPDATE_NOWAIT = text(
    "SELECT corpus_id FROM knowledge_base.knowledge_corpus "
    "WHERE corpus_id = :id FOR UPDATE NOWAIT"
)
FOR_SHARE_NOWAIT = text(
    "SELECT corpus_id FROM knowledge_base.knowledge_corpus "
    "WHERE corpus_id = :id FOR SHARE NOWAIT"
)


async def delete_corpus(session_factory: SessionFactory) -> None:
    async with session_factory() as session, session.begin():
        await session.execute(text("DELETE FROM knowledge_base.indexations"))
        await session.execute(text("DELETE FROM knowledge_base.search_indexes"))
        await session.execute(text("DELETE FROM knowledge_base.knowledge_corpus"))


@pytest.fixture(autouse=True)
async def clean_corpus(session_factory: SessionFactory) -> AsyncIterator[None]:
    await delete_corpus(session_factory)
    yield
    await delete_corpus(session_factory)


def new_corpus(index_id: UUID) -> KnowledgeCorpus:
    corpus = KnowledgeCorpus.empty()
    corpus.provision_index(
        index_id=index_id, profile=PROFILE, chunking_policy=policy(), now=NOW
    )
    corpus.activate_index(index_id, now=NOW, coverage_complete=True)
    return corpus


async def add_corpus(session_factory: SessionFactory, index_id: UUID) -> None:
    async with session_factory() as session, session.begin():
        await SqlAlchemyKnowledgeCorpusRepository(session).add(new_corpus(index_id))


async def load(session_factory: SessionFactory) -> KnowledgeCorpus:
    async with session_factory() as session, session.begin():
        corpus = await SqlAlchemyKnowledgeCorpusRepository(session).get_shared()
    assert corpus is not None
    return corpus


async def save(session_factory: SessionFactory, corpus: KnowledgeCorpus) -> None:
    async with session_factory() as session, session.begin():
        await SqlAlchemyKnowledgeCorpusRepository(session).save(corpus)


async def stored(session_factory: SessionFactory) -> tuple[list[UUID], list[UUID]]:
    """(corpus, index) présents en base."""
    async with session_factory() as session:
        corpora = await session.scalars(
            text("SELECT corpus_id FROM knowledge_base.knowledge_corpus")
        )
        corpus_ids = [UUID(str(c)) for c in corpora]
        indexes = await session.scalars(
            text("SELECT index_id FROM knowledge_base.search_indexes")
        )
        return corpus_ids, [UUID(str(i)) for i in indexes]


def sqlstate(error: DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", None)


# --- barrière blue/green ------------------------------------------------------------


async def test_a_shared_read_holds_off_an_exclusive_lock_until_its_commit(
    session_factory: SessionFactory,
) -> None:
    await add_corpus(session_factory, uuid4())

    async with session_factory() as holder, holder.begin():
        assert (
            await SqlAlchemyKnowledgeCorpusRepository(holder).get_shared() is not None
        )

        with pytest.raises(DBAPIError) as refused:
            async with session_factory() as other, other.begin():
                await other.execute(FOR_UPDATE_NOWAIT, {"id": CORPUS_ID})
        assert sqlstate(refused.value) == LOCK_NOT_AVAILABLE

        # Les lectures partagées (extraction, publication) restent compatibles.
        async with session_factory() as reader, reader.begin():
            shared = await reader.scalar(FOR_SHARE_NOWAIT, {"id": CORPUS_ID})
        assert UUID(str(shared)) == CORPUS_ID

    async with session_factory() as other, other.begin():
        locked = await other.scalar(FOR_UPDATE_NOWAIT, {"id": CORPUS_ID})
    assert UUID(str(locked)) == CORPUS_ID


async def test_a_read_for_update_holds_off_shared_reads(
    session_factory: SessionFactory,
) -> None:
    await add_corpus(session_factory, uuid4())

    async with session_factory() as holder, holder.begin():
        locked = await SqlAlchemyKnowledgeCorpusRepository(holder).get_for_update()
        assert locked is not None

        with pytest.raises(DBAPIError) as refused:
            async with session_factory() as other, other.begin():
                await other.execute(FOR_SHARE_NOWAIT, {"id": CORPUS_ID})
        assert sqlstate(refused.value) == LOCK_NOT_AVAILABLE


# --- provisionnement concurrent -----------------------------------------------------


async def test_two_concurrent_creations_leave_one_corpus_with_one_index(
    session_factory: SessionFactory,
) -> None:
    both_read = asyncio.Barrier(2)

    async def create(index_id: UUID) -> UUID | None:
        """L'index créé, ou ``None`` si l'autre transaction a gagné."""
        async with session_factory() as session, session.begin():
            corpora = SqlAlchemyKnowledgeCorpusRepository(session)
            assert await corpora.get_for_update() is None
            # Les deux transactions ont vu « aucun corpus » avant d'écrire.
            await both_read.wait()
            try:
                await corpora.add(new_corpus(index_id))
            except CorpusAlreadyExistsError:
                return None
        return index_id

    first, second = uuid4(), uuid4()
    results = await asyncio.gather(create(first), create(second))

    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert await stored(session_factory) == ([CORPUS_ID], winners)
    assert (await load(session_factory)).active_index_id == winners[0]


async def test_concurrent_provisioning_by_the_handler_converges_on_one_index(
    session_factory: SessionFactory,
) -> None:
    provisioner = build_provision_search_index_handler(
        session_factory, FakeVectorIndex()
    )
    command = ProvisionSearchIndex(profile=PROFILE, chunking_policy=policy())

    first, second = await asyncio.gather(
        provisioner.handle(command), provisioner.handle(command)
    )

    assert first == second
    assert await stored(session_factory) == ([CORPUS_ID], [first])
    corpus = await load(session_factory)
    assert corpus.active_index_id == first
    assert [i.status for i in corpus.indexes] == [IndexStatus.ACTIVE]


# --- verrou optimiste ---------------------------------------------------------------


async def test_saving_a_stale_copy_is_refused_and_writes_nothing(
    session_factory: SessionFactory,
) -> None:
    active, kept, lost = uuid4(), uuid4(), uuid4()
    await add_corpus(session_factory, active)
    fresh = await load(session_factory)
    stale = await load(session_factory)

    fresh.provision_index(
        index_id=kept, profile=PROFILE, chunking_policy=policy(), now=NOW
    )
    await save(session_factory, fresh)
    stale.provision_index(
        index_id=lost, profile=PROFILE, chunking_policy=policy(), now=NOW
    )

    with pytest.raises(CorpusModifiedConcurrentlyError):
        await save(session_factory, stale)

    reloaded = await load(session_factory)
    assert reloaded.version == fresh.version + 1
    assert {i.index_id for i in reloaded.indexes} == {active, kept}
    assert reloaded.active_index_id == active
