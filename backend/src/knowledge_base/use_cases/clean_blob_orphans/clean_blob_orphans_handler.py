"""``CleanBlobOrphans`` : suppression des originaux qu'aucun document non
purgé ne porte (ADR 0012 point 6 et « Conséquences » : le dépôt précède la
transaction d'import, un échec laisse un fichier sans version).

Un blob est supprimé si et seulement si il a plus de ``grace`` ET qu'aucun
document non purgé ne le référence (``BlobReferences.holders`` vide). Un blob
jeune est protégé : c'est peut-être le dépôt d'un import en cours. Le cas
typique d'un orphelin référencé seulement par un tombstone purgé : un import
refusé (409) du contenu d'un document purgé redépose le fichier avant sa
transaction.

Course avec un import du même contenu (revue S7) : l'import dépose le
fichier (nouvelle révision) puis commite sa version. Entre l'inventaire, la
lecture des détenteurs et la suppression, un tel import ne doit pas perdre
son blob : ``DocumentBlobEraser.delete_if_unchanged`` retire le fichier de son
chemin, ne le supprime que s'il n'a pas été redéposé depuis l'inventaire, et
relit les détenteurs une fois le fichier retiré (un import qui aurait
référencé le blob sans le redéposer). La première lecture des détenteurs
reste : elle évite de déplacer les blobs référencés, qui sont la règle.

Répétable sans effet. Les temporaires d'un dépôt en cours (``tmp/``) ne sont
pas des blobs : non traités ici. Le déclenchement périodique est celui de la
réconciliation (S8).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from functools import partial

from knowledge_base.domain.providers.blob_references import BlobReferences
from knowledge_base.domain.providers.document_blob_eraser import DocumentBlobEraser
from knowledge_base.domain.providers.document_blob_inventory import (
    DocumentBlobInventory,
)
from knowledge_base.domain.value_objects.content_hash import ContentHash
from knowledge_base.use_cases.clean_blob_orphans.clean_blob_orphans_command import (
    CleanBlobOrphans,
)

logger = logging.getLogger(__name__)


class CleanBlobOrphansHandler:
    def __init__(
        self,
        *,
        inventory: DocumentBlobInventory,
        references: BlobReferences,
        eraser: DocumentBlobEraser,
        clock: Callable[[], datetime],
    ) -> None:
        self._inventory = inventory
        self._references = references
        self._eraser = eraser
        self._clock = clock

    async def handle(self, command: CleanBlobOrphans) -> None:
        now = self._clock()
        examined = deleted = 0
        for blob in await self._inventory.list_blobs():
            examined += 1
            if now - blob.modified_at <= command.grace:
                continue
            if await self._references.holders(blob.content_hash):
                continue
            if await self._eraser.delete_if_unchanged(
                blob.content_hash,
                blob.revision,
                still_unreferenced=partial(self._unreferenced, blob.content_hash),
            ):
                deleted += 1
                logger.info(
                    "original orphelin %s supprimé (déposé le %s)",
                    blob.content_hash.value,
                    blob.modified_at.isoformat(),
                )
        logger.info(
            "nettoyage des orphelins : %d original(aux) examiné(s), %d supprimé(s)",
            examined,
            deleted,
        )

    async def _unreferenced(self, content_hash: ContentHash) -> bool:
        return not await self._references.holders(content_hash)
