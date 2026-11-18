"""Port des originaux laissés en quarantaine (S8 ; ADR 0012, suivi S7, point
(c)) : la suppression conditionnelle d'un orphelin retire d'abord le fichier
de son chemin (``quarantine/``) ; un crash avant sa suppression ou sa remise
en place l'y laisse. La réconciliation le remet à son chemin s'il est encore
détenu, le supprime sinon."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from knowledge_base.domain.value_objects.content_hash import ContentHash


class QuarantinedBlob(Protocol):
    """``name`` identifie le fichier en quarantaine ; ``quarantined_at``
    (aware) date sa mise en quarantaine, au mieux de ce que le système de
    fichiers enregistre."""

    @property
    def name(self) -> str: ...

    @property
    def content_hash(self) -> ContentHash: ...

    @property
    def quarantined_at(self) -> datetime: ...


class BlobQuarantine(Protocol):
    async def list_quarantined(self) -> Sequence[QuarantinedBlob]: ...

    async def restore_quarantined(self, name: str) -> bool:
        """Remet le fichier à son chemin adressé si ce chemin est libre ; faux,
        sans rien changer, si un original y est déjà (dépôt plus récent) ou
        si le fichier a disparu."""
        ...

    async def discard_quarantined(self, name: str) -> bool:
        """Supprime le fichier ; faux s'il était déjà absent."""
        ...
