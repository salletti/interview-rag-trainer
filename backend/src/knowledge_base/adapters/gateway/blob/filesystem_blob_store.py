"""``DocumentBlobStore``, ``DocumentBlobReader``, ``DocumentBlobEraser`` et
``DocumentBlobInventory`` sur un volume local adressé par contenu (ADR 0012).

Le flux est écrit dans ``<root>/tmp/`` (même volume que la destination, pour
que le renommage soit atomique), haché au fil des morceaux, puis renommé vers
``<root>/sha256/<aa>/<bb>/<empreinte>`` après ``fsync`` du fichier ; le
répertoire de destination est ensuite synchronisé pour que le renommage
survive à une coupure. Un fichier n'apparaît donc au chemin final que complet.
Les écritures disque passent par ``asyncio.to_thread`` : la boucle de l'API
n'attend jamais le disque.

Suppression et inventaire (S7) : seul cet adapter supprime un fichier (test
d'architecture ``test_withdrawal_purge_structure``) ; l'inventaire ne voit que
les chemins adressés par contenu, jamais ``tmp/``.

Suppression d'un orphelin (revue S7) : ``put`` remplace un fichier existant
par ``os.replace`` (nouvel inode, date fraîche). Un ``unlink`` sans condition
entre l'inventaire et la suppression effacerait le dépôt d'un import du même
contenu qui vient de commiter : version sans blob, ``BLOB_MISSING`` terminal,
et contenu jamais réimportable (``UNIQUE(content_hash)``). Le fichier est donc
d'abord renommé, atomiquement, vers ``quarantine/<empreinte>-<uuid>`` (même
volume) : un ``put`` ultérieur publie un nouveau fichier au chemin, sans
conflit. Il n'est supprimé que si son inode et sa date (ns) sont ceux de
l'inventaire et que l'appelant confirme, fichier retiré, qu'aucun document ne
le porte ; sinon il revient à son chemin (octets identiques à ceux d'un dépôt
concurrent : écraser l'un par l'autre est sans danger). Pas sous ``tmp/`` :
le renommage garde la date de l'orphelin, plus vieille que le délai de grâce,
et le nettoyage des temporaires (S8) pourrait l'effacer pendant la relecture
des détenteurs, rendant la remise en place impossible.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import tempfile
import uuid
from collections.abc import AsyncIterable, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

from knowledge_base.domain.providers.document_blob_inventory import BlobRevision
from knowledge_base.domain.providers.document_blob_reader import BlobMissingError
from knowledge_base.domain.providers.document_blob_store import (
    BlobTooLargeError,
    StoredBlob,
)
from knowledge_base.domain.value_objects.content_hash import ContentHash

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StoredBlobFile:
    """Élément de l'inventaire : empreinte, date de modification (UTC) du
    fichier, qui date le dépôt, et révision (inode, date en ns) qui
    conditionne sa suppression."""

    content_hash: ContentHash
    modified_at: datetime
    revision: BlobRevision


class FileSystemBlobStore:
    """``DocumentBlobStore`` (import), ``DocumentBlobReader`` (extraction),
    ``DocumentBlobEraser`` et ``DocumentBlobInventory`` (purge, orphelins)."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._addressed = root / "sha256"
        # Les temporaires orphelins (crash pendant un upload) restent
        # repérables ici pour la réconciliation (S8).
        self._staging = root / "tmp"
        self._quarantine_dir = root / "quarantine"

    def _path_of(self, content_hash: ContentHash) -> Path:
        digest = content_hash.value
        return self._addressed / digest[:2] / digest[2:4] / digest

    async def put(self, chunks: AsyncIterable[bytes], *, max_bytes: int) -> StoredBlob:
        staged = await asyncio.to_thread(self._open_staging_file)
        try:
            with staged:
                digest, size = await _copy(chunks, staged, max_bytes)
                await asyncio.to_thread(_flush_to_disk, staged)
            content_hash = ContentHash(digest)
            await asyncio.to_thread(self._publish, Path(staged.name), content_hash)
        except BaseException:
            # BaseException : une annulation (client déconnecté) ne doit pas
            # laisser de temporaire non plus. Suppression synchrone : un nouvel
            # await pourrait être annulé à son tour.
            Path(staged.name).unlink(missing_ok=True)  # noqa: ASYNC240
            raise
        return StoredBlob(content_hash=content_hash, size_bytes=size)

    async def exists(self, content_hash: ContentHash) -> bool:
        return await asyncio.to_thread(self._path_of(content_hash).is_file)

    async def read(self, content_hash: ContentHash) -> bytes:
        """Lu en entier : 20 Mo au plus (ADR 0012), et le parseur a besoin du
        document complet."""
        try:
            return await asyncio.to_thread(self._path_of(content_hash).read_bytes)
        except FileNotFoundError as error:
            raise BlobMissingError(content_hash) from error

    async def delete(self, content_hash: ContentHash) -> bool:
        return await asyncio.to_thread(self._delete, self._path_of(content_hash))

    async def delete_if_unchanged(
        self,
        content_hash: ContentHash,
        observed: BlobRevision,
        *,
        still_unreferenced: Callable[[], Awaitable[bool]],
    ) -> bool:
        target = self._path_of(content_hash)
        quarantined = await asyncio.to_thread(
            self._quarantine, target, content_hash, observed
        )
        if quarantined is None:
            return False
        try:
            unreferenced = await still_unreferenced()
        except BaseException:
            # Synchrone, comme le nettoyage de ``put`` : un nouvel await
            # pourrait être annulé à son tour et laisser le fichier hors de
            # son chemin.
            _restore(quarantined, target)
            raise
        if not unreferenced:
            await asyncio.to_thread(_restore, quarantined, target)
            logger.info(
                "original %s conservé : référencé pendant sa suppression",
                content_hash.value,
            )
            return False
        await asyncio.to_thread(quarantined.unlink)
        return True

    async def list_blobs(self) -> list[StoredBlobFile]:
        return await asyncio.to_thread(self._list_blobs)

    def _quarantine(
        self, target: Path, content_hash: ContentHash, observed: BlobRevision
    ) -> Path | None:
        """Chemin de quarantaine du fichier, ou ``None`` s'il était absent ou
        a changé depuis l'inventaire (il est alors déjà remis en place)."""
        self._quarantine_dir.mkdir(parents=True, exist_ok=True)
        quarantined = self._quarantine_dir / f"{content_hash.value}-{uuid.uuid4().hex}"
        try:
            os.rename(target, quarantined)
        except FileNotFoundError:
            return None
        if _revision(quarantined.stat()) != observed:
            _restore(quarantined, target)
            logger.info(
                "original %s conservé : redéposé depuis l'inventaire",
                content_hash.value,
            )
            return None
        return quarantined

    @staticmethod
    def _delete(path: Path) -> bool:
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        # Les répertoires <aa>/<bb> restent : un import concurrent du même
        # préfixe pourrait y publier au même instant.
        return True

    def _list_blobs(self) -> list[StoredBlobFile]:
        found: list[StoredBlobFile] = []
        for path in sorted(self._addressed.glob("*/*/*")):
            if not path.is_file():
                continue
            try:
                content_hash = ContentHash(path.name)
                status = path.stat()
            except ValueError:
                logger.warning("fichier inattendu dans le volume des blobs : %s", path)
                continue
            except FileNotFoundError:
                continue  # supprimé entre le parcours et la lecture
            if path != self._path_of(content_hash):
                logger.warning("blob hors de son chemin d'adressage : %s", path)
                continue
            found.append(
                StoredBlobFile(
                    content_hash=content_hash,
                    modified_at=datetime.fromtimestamp(status.st_mtime, UTC),
                    revision=_revision(status),
                )
            )
        return found

    def _open_staging_file(self) -> IO[bytes]:
        self._staging.mkdir(parents=True, exist_ok=True)
        return tempfile.NamedTemporaryFile(
            dir=self._staging, prefix="upload-", suffix=".part", delete=False
        )

    def _publish(self, staged: Path, content_hash: ContentHash) -> None:
        target = self._path_of(content_hash)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Atomique, y compris si un import concurrent du même contenu a déjà
        # publié : les deux fichiers sont identiques, l'un remplace l'autre.
        os.replace(staged, target)
        _fsync_directory(target.parent)


async def _copy(
    chunks: AsyncIterable[bytes], staged: IO[bytes], max_bytes: int
) -> tuple[str, int]:
    """Écrit le flux en le hachant ; s'arrête dès que ``max_bytes`` est
    dépassé, sans lire la suite."""
    hasher = hashlib.sha256()
    size = 0
    async for chunk in chunks:
        size += len(chunk)
        if size > max_bytes:
            raise BlobTooLargeError(max_bytes)
        hasher.update(chunk)
        await asyncio.to_thread(staged.write, chunk)
    return hasher.hexdigest(), size


def _flush_to_disk(staged: IO[bytes]) -> None:
    staged.flush()
    os.fsync(staged.fileno())


def _revision(status: os.stat_result) -> BlobRevision:
    return BlobRevision(inode=status.st_ino, modified_ns=status.st_mtime_ns)


def _restore(quarantined: Path, target: Path) -> None:
    os.replace(quarantined, target)
    _fsync_directory(target.parent)


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
