"""Échecs déterministes de l'extraction d'un original (ADR 0008).

Un nouvel essai sur le même contenu échouerait pareillement : le handler de
travail en fait un échec terminal, sans retry.
"""

from __future__ import annotations

from typing import Final

EMPTY_DOCUMENT: Final = "EMPTY_DOCUMENT"
INVALID_DOCUMENT: Final = "INVALID_DOCUMENT"


class DocumentParseError(Exception):
    """``code`` : ``EMPTY_DOCUMENT`` (aucun contenu) ou ``INVALID_DOCUMENT``
    (contenu illisible ou format non pris en charge)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
