"""Inventaire et suppression des temporaires de dépôt abandonnés (S8 ;
ADR 0012, suivi S2 : « temporaires sous ``KB_BLOB_ROOT/tmp/`` repérables par la
réconciliation »), sur un vrai répertoire temporaire.

API figée (``FileSystemBlobStore``, port ``StagedUploadStore`` de
``knowledge_base.domain.providers.staged_upload_store``) :

- ``await store.list_staged() -> Sequence[StagedUpload]`` : un élément par
  fichier de ``root/tmp/`` (``name``: nom du fichier seul, ``modified_at``:
  ``datetime`` aware UTC). Les blobs adressés (``root/sha256/``) n'en font pas
  partie. Dossier absent : séquence vide.
- ``await store.delete_staged(name) -> bool`` : supprime ``root/tmp/<name>`` ;
  vrai s'il existait, faux s'il était déjà absent (rejouable). Un nom qui
  contient un séparateur de chemin ou ``..`` est refusé (``ValueError``) sans
  rien supprimer : seul ``tmp/`` est concerné.
- La suppression de fichiers reste confinée à ``adapters/gateway/blob/``
  (``test_withdrawal_purge_structure``).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from knowledge_base.adapters.gateway.blob.filesystem_blob_store import (
    FileSystemBlobStore,
)


async def one_chunk(data: bytes) -> AsyncIterator[bytes]:
    yield data


def stage(root: Path, name: str, content: bytes = b"partiel") -> Path:
    path = root / "tmp" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


async def test_the_staged_files_are_listed_with_their_modification_date(
    tmp_path: Path,
) -> None:
    store = FileSystemBlobStore(tmp_path)
    old = stage(tmp_path, "upload-a.part")
    stage(tmp_path, "upload-b.part")
    os.utime(old, (1_700_000_000, 1_700_000_000))

    listed = {entry.name: entry.modified_at for entry in await store.list_staged()}

    assert set(listed) == {"upload-a.part", "upload-b.part"}
    assert listed["upload-a.part"] == datetime.fromtimestamp(1_700_000_000, UTC)
    assert listed["upload-a.part"].tzinfo is not None


async def test_a_stored_blob_is_not_a_staged_file(tmp_path: Path) -> None:
    store = FileSystemBlobStore(tmp_path)
    await store.put(one_chunk(b"contenu"), max_bytes=1000)
    stage(tmp_path, "upload-a.part")

    assert [e.name for e in await store.list_staged()] == ["upload-a.part"]
    assert len(await store.list_blobs()) == 1


async def test_without_a_staging_directory_there_is_nothing_to_list(
    tmp_path: Path,
) -> None:
    assert list(await FileSystemBlobStore(tmp_path).list_staged()) == []


async def test_deleting_a_staged_file_removes_only_that_file(tmp_path: Path) -> None:
    store = FileSystemBlobStore(tmp_path)
    doomed = stage(tmp_path, "upload-a.part")
    kept = stage(tmp_path, "upload-b.part")

    assert await store.delete_staged("upload-a.part") is True

    assert not doomed.exists()
    assert kept.is_file()


async def test_deleting_an_absent_staged_file_is_false_and_not_an_error(
    tmp_path: Path,
) -> None:
    store = FileSystemBlobStore(tmp_path)
    stage(tmp_path, "upload-a.part")
    await store.delete_staged("upload-a.part")

    assert await store.delete_staged("upload-a.part") is False
    assert await store.delete_staged("jamais-vu.part") is False


@pytest.mark.parametrize(
    "name", ["../sha256/ab/cd/blob", "sub/upload.part", "..", "/etc/passwd", ""]
)
async def test_a_name_that_leaves_the_staging_directory_is_refused(
    tmp_path: Path, name: str
) -> None:
    store = FileSystemBlobStore(tmp_path)
    outside = tmp_path / "sha256" / "ab" / "cd" / "blob"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"intact")

    with pytest.raises(ValueError):
        await store.delete_staged(name)

    assert outside.read_bytes() == b"intact"
