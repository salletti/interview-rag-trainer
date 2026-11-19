"""Refus de ``ActivateSearchIndex`` propres au cas d'usage (ceux de
l'agrégat, I10 et I11, sont dans ``domain/exceptions/corpus_errors``)."""

from __future__ import annotations

from collections.abc import Collection
from uuid import UUID


class SynchronizationPendingError(Exception):
    """Couverture complète, mais une synchronisation de visibilité est encore
    en attente pour des documents publiés : leurs points dans l'index
    candidat peuvent être invisibles (écrits ``visible=false``, §11). Activer
    maintenant retirerait ces documents des résultats jusqu'à la
    synchronisation ; réessayer quand les items ``sync_visibility`` sont
    terminés."""

    def __init__(self, index_id: UUID, document_ids: Collection[UUID]) -> None:
        shown = ", ".join(str(d) for d in sorted(document_ids)[:5])
        more = len(document_ids) - 5
        suffix = f" (et {more} autre(s))" if more > 0 else ""
        super().__init__(
            f"index {index_id} : synchronisation de visibilité en attente pour "
            f"{len(document_ids)} document(s) publié(s) : {shown}{suffix} ; "
            "activation refusée, à réessayer une fois ces items terminés"
        )
        self.index_id = index_id
        self.document_ids = frozenset(document_ids)
