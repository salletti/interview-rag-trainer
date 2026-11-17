"""Adapter système de fichiers de ``DocumentBlobStore`` (ADR 0012, S2).

API figée : ``FileSystemBlobStore(root: Path)`` (module
``knowledge_base.adapters.gateway.blob.filesystem_blob_store``) ;
``await store.put(chunks, *, max_bytes) -> StoredBlob(content_hash,
size_bytes)`` ; ``await store.exists(content_hash)``.

Contrat : le flux est écrit dans un fichier temporaire situé SOUS ``root``
(même volume), l'empreinte SHA-256 est calculée au fil des morceaux, puis le
fichier est renommé atomiquement vers ``root/sha256/<aa>/<bb>/<hex>``. Aucun
fichier au chemin final n'est visible tant que le flux n'est pas terminé ;
dépassement de ``max_bytes`` ou erreur du flux : ``BlobTooLargeError`` ou
l'erreur d'origine, et plus aucun fichier sous ``root``. Le chemin est
calculé ici indépendamment de l'adapter : il fait partie de la spécification.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from knowledge_base.adapters.gateway.blob.filesystem_blob_store import (
    FileSystemBlobStore,
)
from knowledge_base.domain.providers.document_blob_store import BlobTooLargeError
from knowledge_base.domain.value_objects.content_hash import ContentHash

LIMIT = 10_000_000


def final_path(root: Path, data: bytes) -> Path:
    digest = hashlib.sha256(data).hexdigest()
    return root / "sha256" / digest[:2] / digest[2:4] / digest


def files_under(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


async def chunked(data: bytes, size: int) -> AsyncIterator[bytes]:
    for start in range(0, len(data), size):
        yield data[start : start + size]
        await asyncio.sleep(0)


async def test_put_returns_the_sha256_and_the_size(tmp_path: Path) -> None:
    data = b"contenu du document\n" * 50

    stored = await FileSystemBlobStore(tmp_path).put(chunked(data, 13), max_bytes=LIMIT)

    assert stored.content_hash == ContentHash(hashlib.sha256(data).hexdigest())
    assert stored.size_bytes == len(data)


async def test_the_blob_lands_at_the_content_addressed_path(tmp_path: Path) -> None:
    data = b"# Titre\n\ntexte\n"

    await FileSystemBlobStore(tmp_path).put(chunked(data, 4), max_bytes=LIMIT)

    assert files_under(tmp_path) == [final_path(tmp_path, data)]
    assert final_path(tmp_path, data).read_bytes() == data


async def test_a_large_content_streamed_in_chunks_is_hashed_across_boundaries(
    tmp_path: Path,
) -> None:
    data = bytes(range(256)) * 20_000  # ~5 Mo, morceaux de 64 Kio - 1
    chunk_size = 64 * 1024 - 1

    stored = await FileSystemBlobStore(tmp_path).put(
        chunked(data, chunk_size), max_bytes=LIMIT
    )

    assert stored.content_hash.value == hashlib.sha256(data).hexdigest()
    assert stored.size_bytes == len(data)
    assert final_path(tmp_path, data).read_bytes() == data


async def test_no_file_is_visible_at_the_final_path_before_the_stream_ends(
    tmp_path: Path,
) -> None:
    data = b"abcdefghij" * 100
    target = final_path(tmp_path, data)
    observations: list[tuple[bool, bool]] = []

    async def watching() -> AsyncIterator[bytes]:
        for start in range(0, len(data), 100):
            yield data[start : start + 100]
            observations.append((target.exists(), bool(files_under(tmp_path))))

    await FileSystemBlobStore(tmp_path).put(watching(), max_bytes=LIMIT)

    # Pendant l'écriture : jamais de fichier final, mais un temporaire sous root.
    assert observations
    assert all(final_exists is False for final_exists, _ in observations[:-1])
    assert all(temp_present for _, temp_present in observations[1:-1])
    assert target.read_bytes() == data
    assert files_under(tmp_path) == [target]


async def test_putting_the_same_content_twice_is_idempotent(tmp_path: Path) -> None:
    data = b"meme contenu"
    store = FileSystemBlobStore(tmp_path)

    first = await store.put(chunked(data, 5), max_bytes=LIMIT)
    second = await store.put(chunked(data, 3), max_bytes=LIMIT)

    assert second == first
    assert files_under(tmp_path) == [final_path(tmp_path, data)]
    assert final_path(tmp_path, data).read_bytes() == data


async def test_concurrent_puts_of_the_same_content_leave_one_intact_file(
    tmp_path: Path,
) -> None:
    data = b"x" * 50_000
    store = FileSystemBlobStore(tmp_path)

    results = await asyncio.gather(
        *(store.put(chunked(data, 997), max_bytes=LIMIT) for _ in range(5))
    )

    assert len({r.content_hash for r in results}) == 1
    assert files_under(tmp_path) == [final_path(tmp_path, data)]
    assert final_path(tmp_path, data).read_bytes() == data


async def test_different_contents_are_stored_side_by_side(tmp_path: Path) -> None:
    store = FileSystemBlobStore(tmp_path)

    await store.put(chunked(b"un", 1), max_bytes=LIMIT)
    await store.put(chunked(b"deux", 2), max_bytes=LIMIT)

    assert files_under(tmp_path) == sorted(
        [final_path(tmp_path, b"un"), final_path(tmp_path, b"deux")]
    )


async def test_an_empty_content_is_stored(tmp_path: Path) -> None:
    stored = await FileSystemBlobStore(tmp_path).put(chunked(b"", 1), max_bytes=LIMIT)

    assert stored.size_bytes == 0
    assert stored.content_hash.value == hashlib.sha256(b"").hexdigest()
    assert final_path(tmp_path, b"").read_bytes() == b""


async def test_a_content_exactly_at_the_limit_is_stored(tmp_path: Path) -> None:
    data = b"y" * 1000

    stored = await FileSystemBlobStore(tmp_path).put(chunked(data, 300), max_bytes=1000)

    assert stored.size_bytes == 1000
    assert final_path(tmp_path, data).exists()


async def test_a_content_over_the_limit_is_refused_and_leaves_no_file(
    tmp_path: Path,
) -> None:
    data = b"y" * 1001

    with pytest.raises(BlobTooLargeError):
        await FileSystemBlobStore(tmp_path).put(chunked(data, 300), max_bytes=1000)

    assert files_under(tmp_path) == []


async def test_an_oversized_stream_is_not_consumed_to_the_end(tmp_path: Path) -> None:
    produced = 0

    async def endless() -> AsyncIterator[bytes]:
        nonlocal produced
        while produced < 10_000:
            produced += 1
            yield b"z" * 400

    with pytest.raises(BlobTooLargeError):
        await FileSystemBlobStore(tmp_path).put(endless(), max_bytes=1000)

    assert produced <= 4


async def test_a_failing_stream_leaves_no_file_and_propagates(tmp_path: Path) -> None:
    class UploadBrokenError(Exception):
        pass

    async def broken() -> AsyncIterator[bytes]:
        yield b"debut"
        raise UploadBrokenError

    with pytest.raises(UploadBrokenError):
        await FileSystemBlobStore(tmp_path).put(broken(), max_bytes=LIMIT)

    assert files_under(tmp_path) == []


async def test_a_failed_put_does_not_damage_an_existing_blob(tmp_path: Path) -> None:
    data = b"deja stocke"
    store = FileSystemBlobStore(tmp_path)
    await store.put(chunked(data, 4), max_bytes=LIMIT)

    with pytest.raises(BlobTooLargeError):
        await store.put(chunked(data, 4), max_bytes=3)

    assert files_under(tmp_path) == [final_path(tmp_path, data)]
    assert final_path(tmp_path, data).read_bytes() == data


async def test_exists_reflects_the_stored_blobs(tmp_path: Path) -> None:
    store = FileSystemBlobStore(tmp_path)
    present = ContentHash(hashlib.sha256(b"present").hexdigest())
    absent = ContentHash(hashlib.sha256(b"absent").hexdigest())

    await store.put(chunked(b"present", 3), max_bytes=LIMIT)

    assert await store.exists(present) is True
    assert await store.exists(absent) is False


async def test_the_root_is_created_when_missing(tmp_path: Path) -> None:
    root = tmp_path / "volume" / "blobs"

    await FileSystemBlobStore(root).put(chunked(b"data", 2), max_bytes=LIMIT)

    assert final_path(root, b"data").read_bytes() == b"data"
