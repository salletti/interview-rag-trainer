"""Entretien du volume des blobs et des clés d'idempotence (S8 ; ADR 0012
point 6 et suivis S2, S7 ; O15).

Dans cet ordre :

1. quarantaine : un fichier laissé sous ``quarantine/`` par un crash pendant
   la suppression conditionnelle d'un orphelin est remis à son chemin s'il
   est encore détenu et que ce chemin est libre ; sinon supprimé, une fois
   plus vieux que le délai de grâce (sa mise en quarantaine date du crash) ;
2. ``CleanBlobOrphans`` (S7) : originaux qu'aucun document non purgé ne
   porte, plus vieux que le délai de grâce — APRÈS la quarantaine, pour qu'un
   blob détenu remis en place soit vu ;
3. ``CleanStaleUploads`` : temporaires de dépôt abandonnés, même délai ;
4. clés d'idempotence plus vieilles que leur durée de vie (O15).

La reprise de la quarantaine ne court avec aucun nettoyage : celui des
orphelins n'est lancé que par la réconciliation, dont les cycles sont
sérialisés sur la file d'ingestion (un seul processus, ``--concurrency=1``).
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta

from knowledge_base.domain.providers.blob_quarantine import BlobQuarantine
from knowledge_base.domain.providers.blob_references import BlobReferences
from knowledge_base.domain.providers.reconciliation_store import ReconciliationStore
from knowledge_base.domain.services.reconciliation import DiscrepancyKind
from knowledge_base.use_cases.clean_blob_orphans.clean_blob_orphans_command import (
    CleanBlobOrphans,
)
from knowledge_base.use_cases.clean_blob_orphans.clean_blob_orphans_handler import (
    CleanBlobOrphansHandler,
)
from knowledge_base.use_cases.clean_stale_uploads.clean_stale_uploads_command import (
    CleanStaleUploads,
)
from knowledge_base.use_cases.clean_stale_uploads.clean_stale_uploads_handler import (
    CleanStaleUploadsHandler,
)

logger = logging.getLogger(__name__)


class StorageHousekeeping:
    def __init__(
        self,
        *,
        quarantine: BlobQuarantine,
        blob_references: BlobReferences,
        blob_orphans: CleanBlobOrphansHandler,
        stale_uploads: CleanStaleUploadsHandler,
        store: ReconciliationStore,
        clock: Callable[[], datetime],
    ) -> None:
        self._quarantine = quarantine
        self._references = blob_references
        self._blob_orphans = blob_orphans
        self._stale_uploads = stale_uploads
        self._store = store
        self._clock = clock

    async def run(
        self, *, grace: timedelta, idempotency_key_ttl: timedelta
    ) -> Counter[DiscrepancyKind]:
        counts: Counter[DiscrepancyKind] = Counter()
        counts[DiscrepancyKind.QUARANTINED_BLOB] = await self._recover_quarantine(grace)
        counts[DiscrepancyKind.ORPHAN_BLOB] = await self._blob_orphans.handle(
            CleanBlobOrphans(grace=grace)
        )
        counts[DiscrepancyKind.STALE_UPLOAD] = await self._stale_uploads.handle(
            CleanStaleUploads(grace=grace)
        )
        counts[
            DiscrepancyKind.EXPIRED_IDEMPOTENCY_KEY
        ] = await self._store.delete_idempotency_keys_older_than(idempotency_key_ttl)
        return counts

    async def _recover_quarantine(self, grace: timedelta) -> int:
        """Fichiers remis en place ou supprimés."""
        now = self._clock()
        recovered = 0
        for blob in await self._quarantine.list_quarantined():
            held = await self._references.holders(blob.content_hash)
            if held and await self._quarantine.restore_quarantined(blob.name):
                recovered += 1
                logger.warning(
                    "original %s retrouvé en quarantaine et encore détenu : remis "
                    "à son chemin",
                    blob.content_hash.value,
                )
                continue
            if now - blob.quarantined_at <= grace:
                continue
            if await self._quarantine.discard_quarantined(blob.name):
                recovered += 1
                logger.warning(
                    "original %s laissé en quarantaine depuis %s supprimé (%s)",
                    blob.content_hash.value,
                    blob.quarantined_at.isoformat(),
                    "chemin déjà occupé" if held else "plus détenu",
                )
        return recovered
