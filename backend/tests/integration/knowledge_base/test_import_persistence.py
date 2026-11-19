"""Persistance SQLAlchemy de S2 : Unit of Work, dépôt du document, verrou
optimiste, clés d'idempotence (PostgreSQL réel).

API figée (module ``knowledge_base.adapters.gateway.persistence``) :

- ``sqlalchemy_unit_of_work.SqlAlchemyUnitOfWork(session_factory)`` : une
  session et une transaction par ``async with`` ; ``documents`` (dépôt du
  document), ``work_queue`` (``SqlAlchemyWorkQueue`` de S0 sur la MÊME session)
  et ``idempotency_keys``, partagent donc la transaction ; ``await commit()`` ;
  sortie sans commit ou sur exception = rollback.
- ``save(document)`` : ``UPDATE documents SET version = version + 1 ... WHERE
  id = :id AND version = :version_chargee`` ; zéro ligne touchée ->
  ``ConcurrentModificationError(document_id)``, levée par ``save`` lui-même, et
  le document est écrit AVANT ses versions (pas de ligne de version orpheline
  d'un perdant). ``document.version`` relu après commit = valeur + 1.
- Garde-fou global ``UNIQUE(content_hash)`` sur ``document_versions`` (I3 + O2) :
  deux documents distincts avec la même empreinte -> ``ContentHashTakenError``
  (port du dépôt), traduite par l'adapter depuis la violation de
  ``uq_document_versions_content_hash`` : ``IntegrityError`` ne sort pas de la
  persistance (arbitrage S2), le handler rejoue alors sa transaction. La
  contrainte elle-même est vérifiée par ``test_import_migration``.
- Revue S2 I-1 : dans une Unit of Work, la **première** lecture d'un document
  fixe la version du verrou ; une relecture (``get``) ne la rafraîchit pas, et
  ``find_by_content_hash`` rend un modèle de lecture (``ContentOwner``), pas
  l'agrégat.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from knowledge_base.adapters.gateway.persistence.sqlalchemy_unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from knowledge_base.domain.entities.knowledge_document import (
    DocumentStatus,
    KnowledgeDocument,
    VersionStatus,
)
from knowledge_base.domain.providers.idempotency_key_repository import (
    IdempotencyRecord,
)
from knowledge_base.domain.providers.knowledge_document_repository import (
    ConcurrentModificationError,
    ContentHashTakenError,
    ContentOwner,
)
from knowledge_base.domain.value_objects.document_kind import DocumentKind
from runtime.database import SessionFactory
from tests.integration.knowledge_base.import_support import clean_documents
from tests.unit.knowledge_base.domain.builders import (
    DOCUMENT_ID,
    content_hash,
    extract,
    import_next,
    new_document,
    source,
    version_id,
)

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def empty_documents(session_factory: SessionFactory) -> None:
    await clean_documents(session_factory)


async def persist(factory: SessionFactory, document: KnowledgeDocument) -> None:
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.documents.add(document)
        await uow.commit()


async def load(factory: SessionFactory, document_id: UUID) -> KnowledgeDocument:
    async with SqlAlchemyUnitOfWork(factory) as uow:
        document = await uow.documents.get(document_id)
    assert document is not None
    return document


async def test_a_committed_document_is_read_back_identically(
    session_factory: SessionFactory,
) -> None:
    original = new_document(1)
    import_next(original, 2)
    await persist(session_factory, original)

    loaded = await load(session_factory, DOCUMENT_ID)

    assert loaded.document_id == DOCUMENT_ID
    assert (loaded.title, loaded.kind, loaded.topics, loaded.language) == (
        "Python avancé",
        DocumentKind.COURSE,
        ("python", "asyncio"),
        "fr",
    )
    assert loaded.status is original.status
    assert loaded.published_version_id is None
    assert loaded.visibility_revision == 0
    assert loaded.versions == original.versions


async def test_an_unknown_document_is_none(session_factory: SessionFactory) -> None:
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.documents.get(uuid4()) is None


async def test_leaving_the_unit_of_work_without_commit_rolls_everything_back(
    session_factory: SessionFactory,
) -> None:
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await uow.documents.add(new_document(1))
        await uow.work_queue.enqueue(
            "extract_version", f"extract_version:{version_id(1)}"
        )
        await uow.idempotency_keys.add(
            IdempotencyRecord("k", "f", DOCUMENT_ID, version_id(1), 1, True)
        )

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.documents.get(DOCUMENT_ID) is None
        assert await uow.idempotency_keys.get("k") is None
        assert await uow.work_queue.cancel(f"extract_version:{version_id(1)}") is False


async def test_document_work_item_and_key_commit_together(
    session_factory: SessionFactory,
) -> None:
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await uow.documents.add(new_document(1))
        await uow.work_queue.enqueue(
            "extract_version", f"extract_version:{version_id(1)}"
        )
        await uow.idempotency_keys.add(
            IdempotencyRecord("k", "f", DOCUMENT_ID, version_id(1), 1, True)
        )
        await uow.commit()

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.documents.get(DOCUMENT_ID) is not None
        assert await uow.idempotency_keys.get("k") is not None
        assert await uow.work_queue.cancel(f"extract_version:{version_id(1)}") is True
        await uow.commit()


async def test_find_by_content_hash_matches_any_version_of_any_document(
    session_factory: SessionFactory,
) -> None:
    document = new_document(1)
    import_next(document, 2)  # la version 1 est DISCARDED
    await persist(session_factory, document)

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        by_discarded = await uow.documents.find_by_content_hash(content_hash(1))
        by_received = await uow.documents.find_by_content_hash(content_hash(2))
        unknown = await uow.documents.find_by_content_hash(content_hash(99))

    assert by_discarded == ContentOwner(
        document_id=DOCUMENT_ID,
        document_status=DocumentStatus.ACTIVE,
        version_id=version_id(1),
        version_number=1,
    )
    assert by_received == ContentOwner(
        document_id=DOCUMENT_ID,
        document_status=DocumentStatus.ACTIVE,
        version_id=version_id(2),
        version_number=2,
    )
    assert unknown is None


async def commit_a_concurrent_extraction(factory: SessionFactory) -> None:
    """Une autre transaction fait passer la version 1 à ``EXTRACTED`` : le
    document change (verrou + 1) sans nouvelle version."""
    async with SqlAlchemyUnitOfWork(factory) as other:
        document = await other.documents.get(DOCUMENT_ID)
        assert document is not None
        extract(document, 1)
        await other.documents.save(document)
        await other.commit()


async def test_a_second_read_does_not_refresh_the_lock_of_the_first_instance(
    session_factory: SessionFactory,
) -> None:
    await persist(session_factory, new_document(1))

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        first_read = await uow.documents.get(DOCUMENT_ID)
        assert first_read is not None
        await commit_a_concurrent_extraction(session_factory)
        second_read = await uow.documents.get(DOCUMENT_ID)
        assert second_read is not None
        # READ COMMITTED : la relecture voit le commit concurrent.
        assert second_read.versions[0].status is VersionStatus.EXTRACTED
        import_next(first_read, 2)

        with pytest.raises(ConcurrentModificationError):
            await uow.documents.save(first_read)

    loaded = await load(session_factory, DOCUMENT_ID)
    assert [(v.number, v.status) for v in loaded.versions] == [
        (1, VersionStatus.EXTRACTED)
    ]


async def test_a_lookup_by_content_hash_does_not_refresh_the_lock(
    session_factory: SessionFactory,
) -> None:
    await persist(session_factory, new_document(1))

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        document = await uow.documents.get(DOCUMENT_ID)
        assert document is not None
        await commit_a_concurrent_extraction(session_factory)
        owner = await uow.documents.find_by_content_hash(content_hash(1))
        assert owner is not None
        assert owner.version_id == version_id(1)
        import_next(document, 2)

        with pytest.raises(ConcurrentModificationError):
            await uow.documents.save(document)

    loaded = await load(session_factory, DOCUMENT_ID)
    assert [(v.number, v.status) for v in loaded.versions] == [
        (1, VersionStatus.EXTRACTED)
    ]


async def test_save_persists_the_new_version_and_the_discarded_one(
    session_factory: SessionFactory,
) -> None:
    await persist(session_factory, new_document(1))

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        document = await uow.documents.get(DOCUMENT_ID)
        assert document is not None
        import_next(document, 2)
        await uow.documents.save(document)
        await uow.commit()

    loaded = await load(session_factory, DOCUMENT_ID)
    assert [(v.number, v.status) for v in loaded.versions] == [
        (1, VersionStatus.DISCARDED),
        (2, VersionStatus.RECEIVED),
    ]
    assert loaded.get_version(version_id(2)).source == source(2)


async def test_a_successful_save_increments_the_optimistic_lock_version(
    session_factory: SessionFactory,
) -> None:
    await persist(session_factory, new_document(1))
    before = (await load(session_factory, DOCUMENT_ID)).version

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        document = await uow.documents.get(DOCUMENT_ID)
        assert document is not None
        import_next(document, 2)
        await uow.documents.save(document)
        await uow.commit()

    assert (await load(session_factory, DOCUMENT_ID)).version == before + 1


async def test_a_stale_save_is_detected_and_only_the_first_writer_wins(
    session_factory: SessionFactory,
) -> None:
    await persist(session_factory, new_document(1))

    async with (
        SqlAlchemyUnitOfWork(session_factory) as first,
        SqlAlchemyUnitOfWork(session_factory) as second,
    ):
        winner = await first.documents.get(DOCUMENT_ID)
        loser = await second.documents.get(DOCUMENT_ID)
        assert winner is not None
        assert loser is not None
        import_next(winner, 2)
        import_next(loser, 3)

        await first.documents.save(winner)
        await first.commit()

        with pytest.raises(ConcurrentModificationError) as raised:
            await second.documents.save(loser)
        assert raised.value.document_id == DOCUMENT_ID

    loaded = await load(session_factory, DOCUMENT_ID)
    assert [v.number for v in loaded.versions] == [1, 2]
    assert loaded.get_version(version_id(2)).source == source(2)


async def test_two_documents_cannot_share_a_content_hash(
    session_factory: SessionFactory,
) -> None:
    await persist(session_factory, new_document(1))
    other = KnowledgeDocument.import_new(
        document_id=uuid4(),
        version_id=uuid4(),
        title="Autre document",
        kind=DocumentKind.TECHNICAL,
        topics=(),
        language="fr",
        source=source(1),
    )

    with pytest.raises(ContentHashTakenError) as raised:
        await persist(session_factory, other)

    assert raised.value.document_id == other.document_id
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.documents.get(other.document_id) is None


async def test_an_idempotency_record_is_read_back(
    session_factory: SessionFactory,
) -> None:
    record = IdempotencyRecord(
        key="key-1",
        fingerprint="f" * 64,
        document_id=DOCUMENT_ID,
        version_id=version_id(1),
        number=1,
        created=False,
    )
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await uow.idempotency_keys.add(record)
        await uow.commit()

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.idempotency_keys.get("key-1") == record
        assert await uow.idempotency_keys.get("key-2") is None
