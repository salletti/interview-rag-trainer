"""``ImportDocumentVersion`` : nouvelle version, I3, I6, O2, retrait (S2).

API figée :

- ``ImportDocumentVersion(document_id, filename, media_type, content,
  idempotency_key=None)`` (module
  ``knowledge_base.use_cases.import_document_version.import_document_version_command``).
- ``ImportDocumentVersionHandler(*, unit_of_work, blob_store, max_upload_bytes,
  id_factory=uuid4, clock=...)`` (module ``..._handler``) ;
  ``await handler.handle(command)`` -> ``ImportDocumentResult`` (celui de
  ``import_document``), toujours ``created=True`` en cas de succès.
- Erreurs : ``knowledge_base.use_cases.errors.DocumentNotFoundError(document_id)``,
  ``DuplicateContentOtherDocumentError`` (``document_id`` et ``version_id`` du
  contenu déjà connu : décision O2, 409 avec l'identifiant existant) ; le
  domaine lève ``DuplicateContentError`` (I3, même document) et
  ``DocumentWithdrawnError``.
- Décision O2 : un import ciblé sur le document B dont l'empreinte appartient
  déjà à un autre document A est refusé ; un import SANS document cible du même
  contenu rend A (cf. ``test_import_document``).
- À chaque import réussi : les versions antérieures non publiées passent
  ``DISCARDED`` (I6) et un ``cancel("extract_version:<version_id>")`` est émis
  pour chacune, dans la même Unit of Work que l'enqueue de la nouvelle.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from knowledge_base.domain.entities.knowledge_document import (
    DocumentStatus,
    KnowledgeDocument,
    VersionStatus,
)
from knowledge_base.domain.exceptions.document_errors import (
    DocumentWithdrawnError,
    DuplicateContentError,
)
from knowledge_base.domain.providers.knowledge_document_repository import (
    ConcurrentModificationError,
)
from knowledge_base.domain.value_objects.document_kind import DocumentKind
from knowledge_base.use_cases.errors import (
    DocumentNotFoundError,
    DuplicateContentOtherDocumentError,
    UnsupportedDocumentTypeError,
)
from knowledge_base.use_cases.import_document.import_document_command import (
    ImportDocument,
)
from knowledge_base.use_cases.import_document_version.import_document_version_command import (  # noqa: E501
    ImportDocumentVersion,
)
from tests.unit.knowledge_base.domain.builders import (
    DOCUMENT_ID,
    restored_document,
    version_id,
)
from tests.unit.knowledge_base.use_cases.fakes import (
    Harness,
    make_harness,
    sha256,
    stream,
)

V1 = b"# Version 1\n"
V2 = b"# Version 2\n"
V3 = b"# Version 3\n"


def new_document_command(content: bytes) -> ImportDocument:
    return ImportDocument(
        title="Python avancé",
        kind=DocumentKind.COURSE,
        topics=(),
        language="fr",
        filename="cours.md",
        media_type="text/markdown",
        content=stream(content),
    )


def version_command(
    document_id: UUID,
    content: bytes,
    *,
    filename: str = "cours.md",
    media_type: str = "text/markdown",
) -> ImportDocumentVersion:
    return ImportDocumentVersion(
        document_id=document_id,
        filename=filename,
        media_type=media_type,
        content=stream(content),
    )


async def document_with_v1(harness: Harness) -> UUID:
    result = await harness.import_document.handle(new_document_command(V1))
    return result.document_id


def statuses(document: KnowledgeDocument) -> dict[int, VersionStatus]:
    return {v.number: v.status for v in document.versions}


async def test_a_new_version_gets_the_next_number_and_is_received() -> None:
    harness = make_harness()
    document_id = await document_with_v1(harness)

    result = await harness.import_version.handle(version_command(document_id, V2))

    assert (result.document_id, result.number, result.created) == (
        document_id,
        2,
        True,
    )
    document = harness.state.documents[document_id]
    assert statuses(document)[2] is VersionStatus.RECEIVED
    assert document.get_version(result.version_id).source.content_hash.value == sha256(
        V2
    )
    assert harness.blobs.blobs.keys() == {sha256(V1), sha256(V2)}


async def test_the_new_version_is_enqueued_for_extraction() -> None:
    harness = make_harness()
    document_id = await document_with_v1(harness)

    result = await harness.import_version.handle(version_command(document_id, V2))

    item = harness.pending_item_for(result.version_id)
    assert item is not None
    assert (item.kind, item.status) == ("extract_version", "PENDING")
    assert item.payload == {
        "document_id": str(document_id),
        "version_id": str(result.version_id),
    }


async def test_the_previous_received_version_is_discarded_and_its_item_cancelled() -> (
    None
):
    harness = make_harness()
    first = await harness.import_document.handle(new_document_command(V1))

    second = await harness.import_version.handle(version_command(first.document_id, V2))

    document = harness.state.documents[first.document_id]
    assert statuses(document) == {
        1: VersionStatus.DISCARDED,
        2: VersionStatus.RECEIVED,
    }
    old_item = harness.pending_item_for(first.version_id)
    assert old_item is not None
    assert old_item.status == "CANCELLED"
    new_item = harness.pending_item_for(second.version_id)
    assert new_item is not None
    assert new_item.status == "PENDING"


async def test_every_unpublished_older_version_is_discarded_and_cancelled() -> None:
    harness = make_harness()
    first = await harness.import_document.handle(new_document_command(V1))
    second = await harness.import_version.handle(version_command(first.document_id, V2))

    third = await harness.import_version.handle(version_command(first.document_id, V3))

    assert third.number == 3
    document = harness.state.documents[first.document_id]
    assert statuses(document) == {
        1: VersionStatus.DISCARDED,
        2: VersionStatus.DISCARDED,
        3: VersionStatus.RECEIVED,
    }
    cancelled = {
        key for key, item in harness.state.items.items() if item.status == "CANCELLED"
    }
    assert cancelled == {
        f"extract_version:{first.version_id}",
        f"extract_version:{second.version_id}",
    }


async def test_a_published_version_is_neither_discarded_nor_cancelled() -> None:
    harness = make_harness()
    harness.state.documents[DOCUMENT_ID] = restored_document(
        [(1, VersionStatus.PUBLISHED), (2, VersionStatus.EXTRACTED)], published=1
    )

    result = await harness.import_version.handle(version_command(DOCUMENT_ID, V3))

    document = harness.state.documents[DOCUMENT_ID]
    assert statuses(document) == {
        1: VersionStatus.PUBLISHED,
        2: VersionStatus.DISCARDED,
        3: VersionStatus.RECEIVED,
    }
    assert harness.state.cancel_calls == [f"extract_version:{version_id(2)}"]
    assert result.number == 3


async def test_the_cancellations_are_issued_for_the_discarded_versions_only() -> None:
    harness = make_harness()
    first = await harness.import_document.handle(new_document_command(V1))
    harness.state.cancel_calls.clear()

    await harness.import_version.handle(version_command(first.document_id, V2))

    assert harness.state.cancel_calls == [f"extract_version:{first.version_id}"]


async def test_an_unknown_document_is_refused() -> None:
    harness = make_harness()
    unknown = uuid4()

    with pytest.raises(DocumentNotFoundError) as raised:
        await harness.import_version.handle(version_command(unknown, V2))

    assert raised.value.document_id == unknown
    assert harness.state.documents == {}
    assert harness.state.items == {}


async def test_a_withdrawn_document_refuses_new_versions_and_changes_nothing() -> None:
    harness = make_harness()
    harness.state.documents[DOCUMENT_ID] = restored_document(
        [(1, VersionStatus.PUBLISHED)],
        published=1,
        status=DocumentStatus.WITHDRAWN,
    )

    with pytest.raises(DocumentWithdrawnError):
        await harness.import_version.handle(version_command(DOCUMENT_ID, V2))

    assert len(harness.state.documents[DOCUMENT_ID].versions) == 1
    assert harness.state.items == {}
    assert harness.state.cancel_calls == []


async def test_a_content_already_in_the_same_document_is_refused_by_i3() -> None:
    harness = make_harness()
    first = await harness.import_document.handle(new_document_command(V1))
    await harness.import_version.handle(version_command(first.document_id, V2))

    with pytest.raises(DuplicateContentError) as raised:
        await harness.import_version.handle(version_command(first.document_id, V1))

    assert raised.value.existing_version_id == first.version_id
    document = harness.state.documents[first.document_id]
    assert statuses(document) == {
        1: VersionStatus.DISCARDED,
        2: VersionStatus.RECEIVED,
    }


async def test_a_content_owned_by_another_document_is_refused_with_its_id() -> None:
    harness = make_harness()
    owner = await harness.import_document.handle(new_document_command(V1))
    target = await harness.import_document.handle(new_document_command(V2))

    with pytest.raises(DuplicateContentOtherDocumentError) as raised:
        await harness.import_version.handle(version_command(target.document_id, V1))

    assert raised.value.document_id == owner.document_id
    assert raised.value.version_id == owner.version_id
    assert len(harness.state.documents[target.document_id].versions) == 1
    assert len(harness.state.documents[owner.document_id].versions) == 1
    assert len(harness.state.items) == 2


async def test_a_refused_duplicate_cancels_nothing() -> None:
    harness = make_harness()
    await harness.import_document.handle(new_document_command(V1))
    target = await harness.import_document.handle(new_document_command(V2))
    harness.state.cancel_calls.clear()

    with pytest.raises(DuplicateContentOtherDocumentError):
        await harness.import_version.handle(version_command(target.document_id, V1))

    assert harness.state.cancel_calls == []
    item = harness.pending_item_for(target.version_id)
    assert item is not None
    assert item.status == "PENDING"


async def test_a_lock_lost_twice_surfaces_and_persists_nothing() -> None:
    # Le premier verrou perdu est rejoué (test_import_race) ; le second remonte.
    harness = make_harness()
    first = await harness.import_document.handle(new_document_command(V1))
    harness.state.conflict_on_save = True

    with pytest.raises(ConcurrentModificationError):
        await harness.import_version.handle(version_command(first.document_id, V2))

    document = harness.state.documents[first.document_id]
    assert statuses(document) == {1: VersionStatus.RECEIVED}
    assert list(harness.state.items) == [f"extract_version:{first.version_id}"]
    assert harness.state.items[f"extract_version:{first.version_id}"].status == (
        "PENDING"
    )


async def test_a_failed_commit_keeps_the_previous_version_and_its_item_intact() -> None:
    harness = make_harness()
    first = await harness.import_document.handle(new_document_command(V1))
    harness.state.fail_commit = True

    with pytest.raises(RuntimeError):
        await harness.import_version.handle(version_command(first.document_id, V2))

    document = harness.state.documents[first.document_id]
    assert statuses(document) == {1: VersionStatus.RECEIVED}
    assert harness.state.items[f"extract_version:{first.version_id}"].status == (
        "PENDING"
    )
    assert len(harness.state.items) == 1
    assert sha256(V2) in harness.blobs.blobs


async def test_a_non_whitelisted_type_is_refused_before_any_write() -> None:
    harness = make_harness()
    first = await harness.import_document.handle(new_document_command(V1))
    puts_before = harness.blobs.put_calls

    with pytest.raises(UnsupportedDocumentTypeError):
        await harness.import_version.handle(
            version_command(
                first.document_id,
                V2,
                filename="cours.pdf",
                media_type="application/pdf",
            )
        )

    assert harness.blobs.put_calls == puts_before
    assert len(harness.state.documents[first.document_id].versions) == 1
