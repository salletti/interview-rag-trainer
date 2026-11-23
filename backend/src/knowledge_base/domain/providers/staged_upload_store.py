"""Port des fichiers temporaires de dépôt (S8 ; ADR 0012, suivi S2 : un import
écrit son flux sous ``tmp/`` puis le renomme ; un crash ou une coupure en
laisse un fichier partiel que personne ne réclamera)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol


class StagedUpload(Protocol):
    """``name`` : nom du fichier seul ; ``modified_at`` (aware) : dernière
    écriture du dépôt, qui sert au délai de grâce."""

    @property
    def name(self) -> str: ...

    @property
    def modified_at(self) -> datetime: ...


class StagedUploadStore(Protocol):
    async def list_staged(self) -> Sequence[StagedUpload]:
        """Temporaires de dépôt ; les originaux adressés n'en font pas partie.
        Aucun temporaire : séquence vide."""
        ...

    async def delete_staged(self, name: str) -> bool:
        """Vrai s'il existait, faux s'il était déjà absent (rejouable). Un nom
        qui sortirait des temporaires (séparateur, ``..``) lève
        ``ValueError`` sans rien supprimer."""
        ...
