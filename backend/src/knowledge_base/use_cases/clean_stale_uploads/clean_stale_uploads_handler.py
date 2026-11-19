"""``CleanStaleUploads`` : suppression des temporaires de dépôt abandonnés (S8 ;
ADR 0012, suivi S2 : temporaires sous ``KB_BLOB_ROOT/tmp/`` repérables par la
réconciliation).

Un import écrit son flux dans un temporaire puis le renomme vers son chemin
adressé ; un crash ou une coupure en laisse un fichier partiel. Un temporaire
est supprimé si et seulement si sa dernière écriture date de plus de
``grace`` (strictement) : un temporaire récent est peut-être un import en
cours. Répétable sans effet.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from knowledge_base.domain.providers.staged_upload_store import StagedUploadStore
from knowledge_base.use_cases.clean_stale_uploads.clean_stale_uploads_command import (
    CleanStaleUploads,
)

logger = logging.getLogger(__name__)


class CleanStaleUploadsHandler:
    def __init__(
        self, *, staging: StagedUploadStore, clock: Callable[[], datetime]
    ) -> None:
        self._staging = staging
        self._clock = clock

    async def handle(self, command: CleanStaleUploads) -> int:
        """Nombre de temporaires supprimés (un fichier disparu entre-temps,
        renommé par l'import qui l'écrivait, ne compte pas)."""
        now = self._clock()
        deleted = 0
        for upload in await self._staging.list_staged():
            if now - upload.modified_at <= command.grace:
                continue
            if await self._staging.delete_staged(upload.name):
                deleted += 1
                logger.info(
                    "temporaire de dépôt abandonné %s supprimé (dernière écriture %s)",
                    upload.name,
                    upload.modified_at.isoformat(),
                )
        return deleted
