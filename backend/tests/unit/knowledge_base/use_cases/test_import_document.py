"""``ImportDocument`` : nouveau document, original, enqueue, refus (S2).

API figée :

- ``ImportDocument(title, kind, topics, language, filename, media_type,
  content, idempotency_key=None)`` (module
  ``knowledge_base.use_cases.import_document.import_document_command``) ;
  ``content`` est un ``AsyncIterable[bytes]`` consommé une seule fois.
- ``ImportDocumentHandler(*, unit_of_work, blob_store, max_upload_bytes,
  id_factory=uuid4, clock=...)`` ; ``await handler.handle(command)`` ->
  ``ImportDocumentResult(document_id, version_id, number, created)``.
  ``created`` est vrai quand une version a été créée (HTTP 202), faux quand le
  même contenu était déjà connu (HTTP 200, réponse idempotente).
- Ordre : contrôle du type (avant toute écriture) -> dépôt du blob (hors
  transaction) -> transaction unique : document/version ``RECEIVED``, item
  ``extract_version`` (``dedup_key`` ``extract_version:<version_id>``, payload
  ``document_id`` et ``version_id``), clé d'idempotence, ``commit``.
- Erreurs (``knowledge_base.use_cases.errors``) :
  ``UnsupportedDocumentTypeError``.
"""

from __future__ import annotations

import pytest

from knowledge_base.domain.entities.knowledge_document import (
    DocumentStatus,
    VersionStatus,
)
from knowledge_base.domain.providers.document_blob_store import BlobTooLargeError
from knowledge_base.domain.value_objects.document_kind import DocumentKind
from knowledge_base.use_cases.errors import UnsupportedDocumentTypeError
from knowledge_base.use_cases.import_document.import_document_command import (
    ImportDocument,
)
from tests.unit.knowledge_base.use_cases.fakes import (
    MAX_UPLOAD_BYTES,
    NOW,
    make_harness,
    sha256,
    stream,
)

CONTENT = b"# Asyncio\n\nLes coroutines ne bloquent pas la boucle.\n"


def command(
    content: bytes = CONTENT,
    *,
    title: str = "Python avancé",
    filename: str = "cours.md",
    media_type: str = "text/markdown",
    idempotency_key: str | None = None,
) -> ImportDocument:
    return ImportDocument(
        title=title,
        kind=DocumentKind.COURSE,
        topics=("python", "asyncio"),
        language="fr",
        filename=filename,
        media_type=media_type,
        content=stream(content),
        idempotency_key=idempotency_key,
    )


async def test_import_creates_an_active_document_with_a_received_version() -> None:
    harness = make_harness()

    result = await harness.import_document.handle(command())

    assert result.created is True
    assert result.number == 1
    document = harness.state.documents[result.document_id]
    assert document.status is DocumentStatus.ACTIVE
    assert (document.title, document.kind, document.topics, document.language) == (
        "Python avancé",
        DocumentKind.COURSE,
        ("python", "asyncio"),
        "fr",
    )
    (version,) = document.versions
    assert version.version_id == result.version_id
    assert version.status is VersionStatus.RECEIVED


async def test_the_version_source_describes_the_stored_original() -> None:
    harness = make_harness()

    result = await harness.import_document.handle(command())

    source = harness.state.documents[result.document_id].versions[0].source
    assert source.content_hash.value == sha256(CONTENT)
    assert source.size_bytes == len(CONTENT)
    assert source.filename == "cours.md"
    assert source.media_type == "text/markdown"
    assert source.imported_at == NOW


async def test_the_original_is_deposited_in_the_blob_store() -> None:
    harness = make_harness()

    await harness.import_document.handle(command())

    assert harness.blobs.blobs == {sha256(CONTENT): CONTENT}


async def test_a_media_type_parameter_is_not_part_of_the_stored_media_type() -> None:
    harness = make_harness()

    result = await harness.import_document.handle(
        command(filename="notes.txt", media_type="text/plain; charset=utf-8")
    )

    source = harness.state.documents[result.document_id].versions[0].source
    assert source.media_type == "text/plain"


@pytest.mark.parametrize(
    ("filename", "media_type"),
    [
        ("cours.md", "text/markdown"),
        ("cours.markdown", "text/markdown"),
        ("notes.txt", "text/plain"),
        ("COURS.MD", "text/markdown"),
    ],
)
async def test_whitelisted_types_and_extensions_are_accepted(
    filename: str, media_type: str
) -> None:
    harness = make_harness()

    result = await harness.import_document.handle(
        command(filename=filename, media_type=media_type)
    )

    assert result.created is True


async def test_an_extract_version_work_item_is_enqueued_pending() -> None:
    harness = make_harness()

    result = await harness.import_document.handle(command())

    item = harness.pending_item_for(result.version_id)
    assert item is not None
    assert item.kind == "extract_version"
    assert item.status == "PENDING"
    assert item.payload == {
        "document_id": str(result.document_id),
        "version_id": str(result.version_id),
    }
    assert len(harness.state.items) == 1


async def test_a_failed_commit_leaves_no_document_and_no_work_item() -> None:
    harness = make_harness()
    harness.state.fail_commit = True

    with pytest.raises(RuntimeError, match="commit"):
        await harness.import_document.handle(command())

    assert harness.state.documents == {}
    assert harness.state.items == {}
    assert harness.state.keys == {}


async def test_a_failed_commit_leaves_the_blob_as_a_findable_orphan() -> None:
    harness = make_harness()
    harness.state.fail_commit = True

    with pytest.raises(RuntimeError):
        await harness.import_document.handle(command())

    assert sha256(CONTENT) in harness.blobs.blobs


async def test_an_enqueue_failure_rolls_back_the_document() -> None:
    harness = make_harness()
    harness.state.fail_enqueue = True

    with pytest.raises(RuntimeError, match="work queue"):
        await harness.import_document.handle(command())

    assert harness.state.documents == {}
    assert harness.state.items == {}


async def test_the_same_content_again_returns_the_existing_document() -> None:
    harness = make_harness()
    first = await harness.import_document.handle(command())

    second = await harness.import_document.handle(command())

    assert (second.document_id, second.version_id, second.number) == (
        first.document_id,
        first.version_id,
        first.number,
    )
    assert second.created is False
    assert len(harness.state.documents) == 1
    assert len(harness.state.documents[first.document_id].versions) == 1


async def test_the_same_content_again_does_not_enqueue_a_second_item() -> None:
    harness = make_harness()
    first = await harness.import_document.handle(command())

    await harness.import_document.handle(command())

    assert list(harness.state.items) == [f"extract_version:{first.version_id}"]


async def test_the_same_content_with_other_metadata_keeps_the_existing_document() -> (
    None
):
    harness = make_harness()
    first = await harness.import_document.handle(command())

    second = await harness.import_document.handle(
        command(title="Un autre titre", filename="autre.md")
    )

    assert second.document_id == first.document_id
    assert harness.state.documents[first.document_id].title == "Python avancé"


async def test_a_different_content_creates_a_distinct_document() -> None:
    harness = make_harness()
    first = await harness.import_document.handle(command())

    second = await harness.import_document.handle(command(CONTENT + b"suite\n"))

    assert second.document_id != first.document_id
    assert second.number == 1
    assert len(harness.state.documents) == 2
    assert len(harness.state.items) == 2


@pytest.mark.parametrize(
    ("filename", "media_type"),
    [
        ("cours.pdf", "application/pdf"),
        ("cours.md", "application/pdf"),
        ("cours.pdf", "text/markdown"),
        ("cours.html", "text/html"),
        ("cours", "text/plain"),
    ],
)
async def test_a_non_whitelisted_type_is_refused_before_any_write(
    filename: str, media_type: str
) -> None:
    harness = make_harness()

    with pytest.raises(UnsupportedDocumentTypeError):
        await harness.import_document.handle(
            command(filename=filename, media_type=media_type)
        )

    assert harness.blobs.put_calls == 0
    assert harness.state.documents == {}
    assert harness.state.items == {}


async def test_a_content_over_the_limit_is_refused_and_nothing_is_kept() -> None:
    harness = make_harness()

    with pytest.raises(BlobTooLargeError):
        await harness.import_document.handle(command(b"x" * (MAX_UPLOAD_BYTES + 1)))

    assert harness.blobs.blobs == {}
    assert harness.state.documents == {}
    assert harness.state.items == {}


async def test_a_content_exactly_at_the_limit_is_accepted() -> None:
    harness = make_harness()

    result = await harness.import_document.handle(command(b"x" * MAX_UPLOAD_BYTES))

    assert result.created is True
