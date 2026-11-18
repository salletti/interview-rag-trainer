"""Port de la purge des données dérivées d'un document retiré
(knowledge-base.md §6 ligne ``purge_document``, §10 ; ADR 0012 point 6)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID


class DocumentPurger(Protocol):
    async def delete_derived_data(
        self, document_id: UUID, *, purged_at: datetime
    ) -> None:
        """Supprime les chunks puis les unités de toutes les versions du
        document et enregistre la date de purge ; les lignes document et
        versions restent (tombstones). Répétable sans effet."""
        ...


class PurgeTransaction(Protocol):
    """Ce que reçoit ``PurgeDocument.apply`` : la transaction de finalisation
    du work item, ouverte (et validée) par l'appelant."""

    @property
    def purger(self) -> DocumentPurger: ...
