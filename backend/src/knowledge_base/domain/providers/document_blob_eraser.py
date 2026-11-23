"""Port de suppression des originaux (ADR 0012 point 6) : la purge d'un
document retiré et le nettoyage des orphelins ne touchent jamais le système
de fichiers directement (test d'architecture ``test_withdrawal_purge_structure``)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from knowledge_base.domain.providers.document_blob_inventory import BlobRevision
from knowledge_base.domain.value_objects.content_hash import ContentHash


class DocumentBlobEraser(Protocol):
    async def delete(self, content_hash: ContentHash) -> bool:
        """Suppression sans condition (purge : l'empreinte d'un document
        retiré ne peut plus être importée, O2). Vrai si un fichier a été
        supprimé, faux s'il était déjà absent : jamais d'erreur pour un
        absent, la purge est rejouable."""
        ...

    async def delete_if_unchanged(
        self,
        content_hash: ContentHash,
        observed: BlobRevision,
        *,
        still_unreferenced: Callable[[], Awaitable[bool]],
    ) -> bool:
        """Suppression d'un orphelin, sans course avec un import du même
        contenu. Le fichier est d'abord retiré de son chemin, de façon
        atomique ; il n'est supprimé que si sa révision est encore
        ``observed`` (aucun dépôt depuis l'inventaire) ET que
        ``still_unreferenced()``, appelé une fois le fichier retiré, est vrai
        (aucun import n'a commité depuis). Sinon, ou si ``still_unreferenced``
        lève (l'erreur remonte), le fichier est remis en place. Vrai si un
        fichier a été supprimé ; faux s'il était absent ou a été conservé."""
        ...
