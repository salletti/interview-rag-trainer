"""``Idempotency-Key`` + empreinte de requête (002 « Erreurs et idempotence »).

API figée :

- ``idempotency_key`` (commande) : facultatif ; absent, aucun enregistrement.
- Empreinte de requête calculée par le handler : opération, document cible,
  titre, nature, thèmes, langue, nom de fichier, type de média et empreinte du
  contenu.
- Même clé + même empreinte -> le résultat enregistré, sans ré-exécuter
  l'import (même ``created``). Même clé + empreinte différente ->
  ``knowledge_base.use_cases.errors.IdempotencyKeyConflictError`` (HTTP 409).
- L'enregistrement de la clé est écrit dans la Unit of Work de l'import : un
  rollback n'en laisse aucun.
"""

from __future__ import annotations

import pytest

from knowledge_base.domain.entities.knowledge_document import VersionStatus
from knowledge_base.domain.value_objects.document_kind import DocumentKind
from knowledge_base.use_cases.errors import IdempotencyKeyConflictError
from knowledge_base.use_cases.import_document.import_document_command import (
    ImportDocument,
)
from knowledge_base.use_cases.import_document_version.import_document_version_command import (  # noqa: E501
    ImportDocumentVersion,
)
from tests.unit.knowledge_base.use_cases.fakes import make_harness, stream

A = b"# Contenu A\n"
B = b"# Contenu B\n"


def new_document(
    content: bytes, key: str | None, *, title: str = "Python avancé"
) -> ImportDocument:
    return ImportDocument(
        title=title,
        kind=DocumentKind.COURSE,
        topics=("python",),
        language="fr",
        filename="cours.md",
        media_type="text/markdown",
        content=stream(content),
        idempotency_key=key,
    )


async def test_the_same_key_and_request_return_the_same_result() -> None:
    harness = make_harness()

    first = await harness.import_document.handle(new_document(A, "key-1"))
    second = await harness.import_document.handle(new_document(A, "key-1"))

    assert second == first
    assert first.created is True
    assert len(harness.state.documents) == 1
    assert len(harness.state.items) == 1


async def test_the_key_is_recorded_with_the_import() -> None:
    harness = make_harness()

    await harness.import_document.handle(new_document(A, "key-1"))

    assert list(harness.state.keys) == ["key-1"]


async def test_no_key_means_no_record() -> None:
    harness = make_harness()

    await harness.import_document.handle(new_document(A, None))

    assert harness.state.keys == {}


async def test_the_same_key_with_another_content_is_a_conflict() -> None:
    harness = make_harness()
    await harness.import_document.handle(new_document(A, "key-1"))

    with pytest.raises(IdempotencyKeyConflictError):
        await harness.import_document.handle(new_document(B, "key-1"))

    assert len(harness.state.documents) == 1
    assert len(harness.state.items) == 1


async def test_the_same_key_with_other_metadata_is_a_conflict() -> None:
    harness = make_harness()
    await harness.import_document.handle(new_document(A, "key-1"))

    with pytest.raises(IdempotencyKeyConflictError):
        await harness.import_document.handle(
            new_document(A, "key-1", title="Un autre titre")
        )


async def test_the_same_key_for_another_target_document_is_a_conflict() -> None:
    harness = make_harness()
    first = await harness.import_document.handle(new_document(A, None))
    other = await harness.import_document.handle(new_document(B, None))
    await harness.import_version.handle(
        ImportDocumentVersion(
            document_id=first.document_id,
            filename="v.md",
            media_type="text/markdown",
            content=stream(b"# v2 de A\n"),
            idempotency_key="key-1",
        )
    )

    with pytest.raises(IdempotencyKeyConflictError):
        await harness.import_version.handle(
            ImportDocumentVersion(
                document_id=other.document_id,
                filename="v.md",
                media_type="text/markdown",
                content=stream(b"# v2 de A\n"),
                idempotency_key="key-1",
            )
        )

    assert len(harness.state.documents[other.document_id].versions) == 1


async def test_a_replay_returns_the_original_result_without_re_executing() -> None:
    harness = make_harness()
    first = await harness.import_document.handle(new_document(A, "key-1"))
    await harness.import_version.handle(
        ImportDocumentVersion(
            document_id=first.document_id,
            filename="v2.md",
            media_type="text/markdown",
            content=stream(B),
        )
    )

    replay = await harness.import_document.handle(new_document(A, "key-1"))

    assert replay == first
    document = harness.state.documents[first.document_id]
    assert [v.number for v in document.versions] == [1, 2]
    assert document.versions[0].status is VersionStatus.DISCARDED
    assert len(harness.state.documents) == 1


async def test_the_replay_of_a_deduplicated_import_stays_not_created() -> None:
    harness = make_harness()
    first = await harness.import_document.handle(new_document(A, None))

    original = await harness.import_document.handle(new_document(A, "key-2"))
    replay = await harness.import_document.handle(new_document(A, "key-2"))

    assert original.created is False
    assert replay == original
    assert replay.document_id == first.document_id


async def test_two_keys_for_the_same_content_resolve_to_one_document() -> None:
    harness = make_harness()
    first = await harness.import_document.handle(new_document(A, "key-1"))

    second = await harness.import_document.handle(new_document(A, "key-2"))

    assert second.document_id == first.document_id
    assert second.created is False
    assert sorted(harness.state.keys) == ["key-1", "key-2"]


async def test_a_rolled_back_import_leaves_no_key_and_the_retry_succeeds() -> None:
    harness = make_harness()
    harness.state.fail_commit = True
    with pytest.raises(RuntimeError):
        await harness.import_document.handle(new_document(A, "key-1"))
    assert harness.state.keys == {}

    harness.state.fail_commit = False
    retry = await harness.import_document.handle(new_document(A, "key-1"))

    assert retry.created is True
    assert len(harness.state.documents) == 1
    assert list(harness.state.keys) == ["key-1"]
