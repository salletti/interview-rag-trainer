"""Courses entre imports concurrents (arbitrage S2 : ``IntegrityError`` ne fuit
pas, revue S2 I-2).

Le dépôt SQL traduit la violation de ``UNIQUE(content_hash)`` en
``ContentHashTakenError`` et celle de la clé primaire des clés d'idempotence
en ``IdempotencyKeyTakenError``. Ici, l'écriture du perdant commite d'abord
l'état du gagnant (invisible pour la Unit of Work déjà ouverte, comme sous
PostgreSQL) puis lève l'exception : le handler doit rejouer sa transaction une
fois et rendre le résultat du gagnant. Même règle pour un verrou optimiste
perdu (``ConcurrentModificationError``) : la seconde tentative relit le
document et la clé.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from knowledge_base.domain.entities.knowledge_document import KnowledgeDocument
from knowledge_base.domain.exceptions.document_errors import (
    DocumentWithdrawnError,
    DuplicateContentError,
)
from knowledge_base.domain.providers.idempotency_key_repository import (
    IdempotencyKeyTakenError,
    IdempotencyRecord,
)
from knowledge_base.domain.providers.knowledge_document_repository import (
    ConcurrentModificationError,
    ContentHashTakenError,
)
from knowledge_base.domain.value_objects.content_hash import ContentHash
from knowledge_base.domain.value_objects.document_kind import DocumentKind
from knowledge_base.domain.value_objects.document_source import DocumentSource
from knowledge_base.use_cases.errors import (
    DuplicateContentOtherDocumentError,
    IdempotencyKeyConflictError,
)
from knowledge_base.use_cases.import_document.import_document_command import (
    ImportDocument,
)
from knowledge_base.use_cases.import_document_version.import_document_version_command import (  # noqa: E501
    ImportDocumentVersion,
)
from knowledge_base.use_cases.import_result import ImportDocumentResult
from tests.unit.knowledge_base.use_cases.fakes import (
    NOW,
    FakeDocuments,
    FakeIdempotencyKeys,
    Harness,
    make_harness,
    sha256,
    stream,
)

CONTENT = b"# Contenu disput\xc3\xa9\n"
OTHER = b"# Autre contenu\n"


def new_document(content: bytes, key: str | None = None) -> ImportDocument:
    return ImportDocument(
        title="Python avancé",
        kind=DocumentKind.COURSE,
        topics=(),
        language="fr",
        filename="cours.md",
        media_type="text/markdown",
        content=stream(content),
        idempotency_key=key,
    )


def winner_document(content: bytes) -> KnowledgeDocument:
    return KnowledgeDocument.import_new(
        document_id=uuid4(),
        version_id=uuid4(),
        title="Gagnant",
        kind=DocumentKind.TECHNICAL,
        topics=(),
        language="fr",
        source=DocumentSource(
            filename="gagnant.md",
            media_type="text/markdown",
            size_bytes=len(content),
            content_hash=ContentHash(sha256(content)),
            imported_at=NOW,
        ),
    )


def lose_next_document_write(
    monkeypatch: pytest.MonkeyPatch, harness: Harness, method: str, content: bytes
) -> KnowledgeDocument:
    """La prochaine écriture ``method`` (``add`` ou ``save``) perd la course
    contre un document gagnant qui porte ``content`` ; les suivantes passent."""
    winner = winner_document(content)
    original = getattr(FakeDocuments, method)
    lost = False

    async def racing(self: FakeDocuments, document: KnowledgeDocument) -> None:
        nonlocal lost
        if not lost:
            lost = True
            harness.state.documents[winner.document_id] = winner
            raise ContentHashTakenError(document.document_id)
        await original(self, document)

    monkeypatch.setattr(FakeDocuments, method, racing)
    return winner


def lose_next_key_write(
    monkeypatch: pytest.MonkeyPatch,
    harness: Harness,
    winner: KnowledgeDocument,
    *,
    same_request: bool,
) -> None:
    original = FakeIdempotencyKeys.add
    lost = False

    async def racing(self: FakeIdempotencyKeys, record: IdempotencyRecord) -> None:
        nonlocal lost
        if not lost:
            lost = True
            harness.state.documents[winner.document_id] = winner
            harness.state.keys[record.key] = IdempotencyRecord(
                key=record.key,
                fingerprint=record.fingerprint if same_request else "autre",
                document_id=winner.document_id,
                version_id=winner.versions[0].version_id,
                number=1,
                created=True,
            )
            raise IdempotencyKeyTakenError(record.key)
        await original(self, record)

    monkeypatch.setattr(FakeIdempotencyKeys, "add", racing)


async def test_losing_a_new_document_race_returns_the_winner_not_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness()
    winner = lose_next_document_write(monkeypatch, harness, "add", CONTENT)

    result = await harness.import_document.handle(new_document(CONTENT))

    assert (result.document_id, result.version_id, result.number) == (
        winner.document_id,
        winner.versions[0].version_id,
        1,
    )
    assert result.created is False
    assert list(harness.state.documents) == [winner.document_id]
    assert harness.state.items == {}


async def test_the_loser_records_its_key_on_the_winner_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness()
    winner = lose_next_document_write(monkeypatch, harness, "add", CONTENT)

    result = await harness.import_document.handle(new_document(CONTENT, "key-1"))

    record = harness.state.keys["key-1"]
    assert (record.document_id, record.created) == (winner.document_id, False)
    assert result.created is False


async def test_losing_a_targeted_race_is_a_duplicate_of_the_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness()
    target = await harness.import_document.handle(new_document(OTHER))
    winner = lose_next_document_write(monkeypatch, harness, "save", CONTENT)

    with pytest.raises(DuplicateContentOtherDocumentError) as raised:
        await harness.import_version.handle(
            ImportDocumentVersion(
                document_id=target.document_id,
                filename="v2.md",
                media_type="text/markdown",
                content=stream(CONTENT),
            )
        )

    assert raised.value.document_id == winner.document_id
    assert raised.value.version_id == winner.versions[0].version_id
    assert len(harness.state.documents[target.document_id].versions) == 1


async def test_losing_a_key_race_for_the_same_request_replays_the_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness()
    winner = winner_document(CONTENT)
    lose_next_key_write(monkeypatch, harness, winner, same_request=True)

    result = await harness.import_document.handle(new_document(OTHER, "key-1"))

    assert (result.document_id, result.created) == (winner.document_id, True)
    assert list(harness.state.documents) == [winner.document_id]


async def test_losing_a_key_race_for_another_request_is_a_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness()
    winner = winner_document(CONTENT)
    lose_next_key_write(monkeypatch, harness, winner, same_request=False)

    with pytest.raises(IdempotencyKeyConflictError):
        await harness.import_document.handle(new_document(OTHER, "key-1"))

    assert list(harness.state.documents) == [winner.document_id]


async def test_a_second_lost_race_surfaces_instead_of_looping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness()
    calls = 0

    async def always_taken(self: FakeDocuments, document: KnowledgeDocument) -> None:
        nonlocal calls
        calls += 1
        raise ContentHashTakenError(document.document_id)

    monkeypatch.setattr(FakeDocuments, "add", always_taken)

    with pytest.raises(ContentHashTakenError):
        await harness.import_document.handle(new_document(CONTENT))

    assert calls == 2
    assert harness.state.documents == {}


async def test_the_content_of_a_withdrawn_document_cannot_be_reimported() -> None:
    harness = make_harness()
    first = await harness.import_document.handle(new_document(CONTENT))
    harness.state.documents[first.document_id].withdraw()

    with pytest.raises(DocumentWithdrawnError) as raised:
        await harness.import_document.handle(new_document(CONTENT, "key-1"))

    assert raised.value.document_id == first.document_id
    assert harness.state.keys == {}
    assert len(harness.state.documents) == 1


def new_version(
    document_id: UUID, content: bytes, key: str | None = None
) -> ImportDocumentVersion:
    return ImportDocumentVersion(
        document_id=document_id,
        filename="v2.md",
        media_type="text/markdown",
        content=stream(content),
        idempotency_key=key,
    )


def lose_next_save_to(
    monkeypatch: pytest.MonkeyPatch, harness: Harness, winner: ImportDocumentVersion
) -> list[ImportDocumentResult]:
    """Juste avant la prochaine écriture ``save``, ``winner`` est importé et
    commité par une autre Unit of Work ; la sauvegarde perd alors le verrou
    optimiste. Les écritures suivantes passent."""
    original = FakeDocuments.save
    won: list[ImportDocumentResult] = []
    lost = False

    async def racing(self: FakeDocuments, document: KnowledgeDocument) -> None:
        nonlocal lost
        if not lost:
            lost = True  # avant l'import gagnant, qui passe aussi par ``save``
            won.append(await harness.import_version.handle(winner))
            raise ConcurrentModificationError(document.document_id)
        await original(self, document)

    monkeypatch.setattr(FakeDocuments, "save", racing)
    return won


async def test_a_lost_lock_with_the_same_key_replays_the_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness()
    target = await harness.import_document.handle(new_document(OTHER))
    won = lose_next_save_to(
        monkeypatch, harness, new_version(target.document_id, CONTENT, "key-1")
    )

    result = await harness.import_version.handle(
        new_version(target.document_id, CONTENT, "key-1")
    )

    assert result == won[0]
    assert result.created is True
    versions = harness.state.documents[target.document_id].versions
    assert [v.number for v in versions] == [1, 2]


async def test_a_lost_lock_on_the_same_content_without_key_is_a_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness()
    target = await harness.import_document.handle(new_document(OTHER))
    won = lose_next_save_to(
        monkeypatch, harness, new_version(target.document_id, CONTENT)
    )

    with pytest.raises(DuplicateContentError) as raised:
        await harness.import_version.handle(new_version(target.document_id, CONTENT))

    assert raised.value.existing_version_id == won[0].version_id
    assert len(harness.state.documents[target.document_id].versions) == 2


async def test_a_lost_lock_on_another_content_imports_on_the_fresh_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness()
    target = await harness.import_document.handle(new_document(OTHER))
    won = lose_next_save_to(
        monkeypatch, harness, new_version(target.document_id, b"# Gagnant\n")
    )

    result = await harness.import_version.handle(
        new_version(target.document_id, CONTENT)
    )

    assert (won[0].number, result.number) == (2, 3)
    document = harness.state.documents[target.document_id]
    assert [v.status.value for v in document.versions] == [
        "DISCARDED",
        "DISCARDED",
        "RECEIVED",
    ]
