"""Port de suppression des originaux (ADR 0012 point 6) : la purge d'un
document retiré et le nettoyage des orphelins ne touchent jamais le système
de fichiers directement (test d'architecture ``test_withdrawal_purge_structure``)."""

from __future__ import annotations

from typing import Protocol

from knowledge_base.domain.value_objects.content_hash import ContentHash


class DocumentBlobEraser(Protocol):
    async def delete(self, content_hash: ContentHash) -> bool:
        """Vrai si un fichier a été supprimé, faux s'il était déjà absent :
        jamais d'erreur pour un absent, la purge est rejouable."""
        ...
