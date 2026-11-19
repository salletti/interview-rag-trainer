"""Refus de ``ActivateSearchIndex`` propres au cas d'usage (ceux de
l'agrégat, I10 et I11, sont dans ``domain/exceptions/corpus_errors``)."""

from __future__ import annotations

from collections.abc import Collection
from uuid import UUID

from knowledge_base.domain.exceptions.corpus_errors import IndexOperationRefusedError


def _listed(document_ids: Collection[UUID]) -> str:
    shown = ", ".join(str(d) for d in sorted(document_ids)[:5])
    more = len(document_ids) - 5
    return shown + (f" (et {more} autre(s))" if more > 0 else "")


class SynchronizationPendingError(IndexOperationRefusedError):
    """Couverture complète, mais une synchronisation de visibilité est encore
    en attente pour des documents publiés : leurs points dans l'index
    candidat peuvent être invisibles (écrits ``visible=false``, §11). Activer
    maintenant retirerait ces documents des résultats jusqu'à la
    synchronisation ; réessayer quand les items ``sync_visibility`` sont
    terminés."""

    def __init__(self, index_id: UUID, document_ids: Collection[UUID]) -> None:
        super().__init__(
            f"index {index_id} : synchronisation de visibilité en attente pour "
            f"{len(document_ids)} document(s) publié(s) : {_listed(document_ids)} "
            "; activation refusée, à réessayer une fois ces items terminés"
        )
        self.index_id = index_id
        self.document_ids = frozenset(document_ids)


class SynchronizationFailedError(IndexOperationRefusedError):
    """Couverture complète, mais la dernière synchronisation de visibilité de
    documents publiés est ``FAILED`` : rien n'a rendu visibles leurs points
    depuis, dans l'index candidat comme ailleurs. Activer maintenant les
    retirerait des résultats ; rejouer l'échec (``POST
    /api/v1/knowledge-base/failures/{item_id}/retry``) ou attendre que la
    réconciliation replanifie la synchronisation, puis réessayer."""

    def __init__(self, index_id: UUID, document_ids: Collection[UUID]) -> None:
        super().__init__(
            f"index {index_id} : dernière synchronisation de visibilité FAILED "
            f"pour {len(document_ids)} document(s) publié(s) : "
            f"{_listed(document_ids)} ; activation refusée, rejouer l'échec "
            "(GET puis POST /api/v1/knowledge-base/failures/{item_id}/retry) ou "
            "attendre la réconciliation, puis réessayer"
        )
        self.index_id = index_id
        self.document_ids = frozenset(document_ids)
