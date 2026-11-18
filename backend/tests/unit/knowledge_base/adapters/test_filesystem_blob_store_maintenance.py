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
  qui sert de date de dépôt pour le délai de grâce) et ``revision``
  (``BlobRevision`` : inode et date en ns). Les temporaires de
  ``root/tmp/`` ne sont pas des blobs. Volume absent ou vide : liste vide.
- ``await store.delete_if_unchanged(content_hash, observed, *,
  still_unreferenced) -> bool`` (revue S7, course avec un import du même
  contenu) : le fichier est renommé hors de son chemin (``root/quarantine/``),
  supprimé si sa révision est encore ``observed`` ET que
  ``still_unreferenced()``, appelé fichier retiré, est vrai ; sinon remis en
  place. Une exception de ``still_unreferenced`` remonte, fichier remis.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from knowledge_base.adapters.gateway.blob.filesystem_blob_store import (
    FileSystemBlobStore,
)
from knowledge_base.domain.providers.document_blob_inventory import BlobRevision
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


# --- suppression conditionnelle d'un orphelin (revue S7) ---------------------------


async def observed(store: FileSystemBlobStore, digest: ContentHash) -> BlobRevision:
    (found,) = [
        b.revision for b in await store.list_blobs() if b.content_hash == digest
    ]
    return found


def answering(
    unreferenced: bool, *, seen: list[bool] | None = None, path: Path | None = None
) -> Callable[[], Awaitable[bool]]:
    """``still_unreferenced`` ; note, s'il est donné, si ``path`` existait au
    moment de l'appel."""

    async def still_unreferenced() -> bool:
        if seen is not None and path is not None:
            seen.append(await asyncio.to_thread(path.exists))
        return unreferenced

    return still_unreferenced


def quarantined(root: Path) -> list[Path]:
    directory = root / "quarantine"
    return sorted(directory.iterdir()) if directory.exists() else []


async def test_list_blobs_gives_the_inode_and_the_nanosecond_date(
    tmp_path: Path,
) -> None:
    store = FileSystemBlobStore(tmp_path)
    digest = await stored(store, b"revision")
    status = final_path(tmp_path, b"revision").stat()

    assert await observed(store, digest) == BlobRevision(
        inode=status.st_ino, modified_ns=status.st_mtime_ns
    )


async def test_an_unchanged_unreferenced_blob_is_deleted(tmp_path: Path) -> None:
    store = FileSystemBlobStore(tmp_path)
    digest = await stored(store, b"orphelin")
    revision = await observed(store, digest)
    seen: list[bool] = []
    path = final_path(tmp_path, b"orphelin")

    removed = await store.delete_if_unchanged(
        digest, revision, still_unreferenced=answering(True, seen=seen, path=path)
    )

    assert removed is True
    assert not path.exists()
    assert quarantined(tmp_path) == []
    # Relu fichier retiré : un import ne peut plus le référencer sans le redéposer.
    assert seen == [False]


async def test_a_blob_redeposited_since_the_inventory_is_put_back(
    tmp_path: Path,
) -> None:
    store = FileSystemBlobStore(tmp_path)
    digest = await stored(store, b"meme contenu")
    revision = await observed(store, digest)
    await stored(store, b"meme contenu")  # import concurrent : os.replace
    fresh = final_path(tmp_path, b"meme contenu").stat().st_ino
    assert fresh != revision.inode

    removed = await store.delete_if_unchanged(
        digest, revision, still_unreferenced=answering(True)
    )

    assert removed is False
    assert await store.read(digest) == b"meme contenu"
    assert final_path(tmp_path, b"meme contenu").stat().st_ino == fresh
    assert quarantined(tmp_path) == []


async def test_a_blob_referenced_once_out_of_its_path_is_put_back(
    tmp_path: Path,
) -> None:
    store = FileSystemBlobStore(tmp_path)
    digest = await stored(store, b"reference entre-temps")
    revision = await observed(store, digest)

    removed = await store.delete_if_unchanged(
        digest, revision, still_unreferenced=answering(False)
    )

    assert removed is False
    assert await observed(store, digest) == revision
    assert quarantined(tmp_path) == []


async def test_a_failing_reference_check_puts_the_blob_back_and_raises(
    tmp_path: Path,
) -> None:
    store = FileSystemBlobStore(tmp_path)
    digest = await stored(store, b"base injoignable")
    revision = await observed(store, digest)

    async def unreachable() -> bool:
        raise ConnectionError("PostgreSQL injoignable")

    with pytest.raises(ConnectionError, match="PostgreSQL injoignable"):
        await store.delete_if_unchanged(
            digest, revision, still_unreferenced=unreachable
        )

    assert await observed(store, digest) == revision
    assert quarantined(tmp_path) == []


async def test_a_conditional_deletion_of_an_absent_blob_asks_nothing(
    tmp_path: Path,
) -> None:
    store = FileSystemBlobStore(tmp_path)
    digest = await stored(store, b"deja parti")
    revision = await observed(store, digest)
    await store.delete(digest)
    seen: list[bool] = []

    removed = await store.delete_if_unchanged(
        digest,
        revision,
        still_unreferenced=answering(True, seen=seen, path=tmp_path),
    )

    assert removed is False
    assert seen == []


async def test_the_quarantine_is_neither_a_blob_nor_a_staging_file(
    tmp_path: Path,
) -> None:
    store = FileSystemBlobStore(tmp_path)
    digest = await stored(store, b"en quarantaine")
    revision = await observed(store, digest)
    listed: list[list[ContentHash]] = []

    async def list_while_quarantined() -> bool:
        listed.append([b.content_hash for b in await store.list_blobs()])
        return False

    await store.delete_if_unchanged(
        digest, revision, still_unreferenced=list_while_quarantined
    )

    assert listed == [[]]
    assert not (tmp_path / "tmp").exists() or list((tmp_path / "tmp").iterdir()) == []
