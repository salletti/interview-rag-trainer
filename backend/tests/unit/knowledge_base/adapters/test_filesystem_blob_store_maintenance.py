"""Suppression et inventaire des blobs sur le volume local (S7 ; ADR 0012
points 2 et 6), sur un vrai répertoire temporaire.

API figée (``FileSystemBlobStore``, en plus de ``put`` / ``exists`` / ``read``) :

- ``await store.delete(content_hash) -> bool`` (port ``DocumentBlobEraser``) :
  supprime le fichier ``root/sha256/<aa>/<bb>/<hex>`` ; vrai s'il existait,
  faux s'il était déjà absent (jamais d'erreur : la purge est rejouable) ;
  n'affecte aucun autre fichier.
- ``await store.list_blobs()`` (port ``DocumentBlobInventory``) : un élément
  par blob adressé par contenu, portant ``content_hash`` (``ContentHash``) et
  ``modified_at`` (``datetime`` aware, UTC : date de modification du fichier,
  qui sert de date de dépôt pour le délai de grâce). Les temporaires de
  ``root/tmp/`` ne sont pas des blobs. Volume absent ou vide : liste vide.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from knowledge_base.adapters.gateway.blob.filesystem_blob_store import (
    FileSystemBlobStore,
)
from knowledge_base.domain.value_objects.content_hash import ContentHash

LIMIT = 10_000_000


async def one_chunk(data: bytes) -> AsyncIterator[bytes]:
    yield data


def final_path(root: Path, data: bytes) -> Path:
    digest = hashlib.sha256(data).hexdigest()
    return root / "sha256" / digest[:2] / digest[2:4] / digest


async def stored(store: FileSystemBlobStore, data: bytes) -> ContentHash:
    return (await store.put(one_chunk(data), max_bytes=LIMIT)).content_hash


async def test_delete_removes_the_file_and_reports_it(tmp_path: Path) -> None:
    store = FileSystemBlobStore(tmp_path)
    digest = await stored(store, b"a supprimer")

    removed = await store.delete(digest)

    assert removed is True
    assert not final_path(tmp_path, b"a supprimer").exists()
    assert await store.exists(digest) is False


async def test_delete_of_an_absent_blob_is_false_and_not_an_error(
    tmp_path: Path,
) -> None:
    store = FileSystemBlobStore(tmp_path)
    digest = await stored(store, b"a supprimer")
    await store.delete(digest)

    assert await store.delete(digest) is False
    assert await store.delete(ContentHash("0" * 64)) is False


async def test_delete_leaves_the_other_blobs_alone(tmp_path: Path) -> None:
    store = FileSystemBlobStore(tmp_path)
    doomed = await stored(store, b"un")
    kept = await stored(store, b"deux")

    await store.delete(doomed)

    assert await store.exists(kept) is True
    assert await store.read(kept) == b"deux"


async def test_list_blobs_gives_each_hash_with_the_modification_date(
    tmp_path: Path,
) -> None:
    store = FileSystemBlobStore(tmp_path)
    first = await stored(store, b"premier")
    second = await stored(store, b"second")
    stamp = datetime(2026, 11, 1, 8, 30, tzinfo=UTC)
    os.utime(final_path(tmp_path, b"premier"), (stamp.timestamp(), stamp.timestamp()))

    listed = {blob.content_hash: blob.modified_at for blob in await store.list_blobs()}

    assert set(listed) == {first, second}
    assert listed[first] == stamp
    assert listed[second].tzinfo is not None
    assert listed[second] > stamp


async def test_list_blobs_ignores_the_staging_files(tmp_path: Path) -> None:
    store = FileSystemBlobStore(tmp_path)
    kept = await stored(store, b"complet")
    staging = tmp_path / "tmp"
    staging.mkdir(exist_ok=True)
    (staging / "upload-abc.part").write_bytes(b"incomplet")

    listed = [blob.content_hash for blob in await store.list_blobs()]

    assert listed == [kept]


async def test_list_blobs_of_an_empty_or_missing_volume_is_empty(
    tmp_path: Path,
) -> None:
    assert list(await FileSystemBlobStore(tmp_path).list_blobs()) == []
    assert list(await FileSystemBlobStore(tmp_path / "absent").list_blobs()) == []
