"""Échecs déterministes de l'extraction d'un original (ADR 0008).

Un nouvel essai sur le même contenu échouerait pareillement : le handler de
travail en fait un échec terminal, sans retry.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class ParseErrorCode(StrEnum):
    """Liste fermée : le handler d'extraction tient chacun de ces codes pour non
    rejouable ; un code hors liste ne doit pas pouvoir y échapper."""

    EMPTY_DOCUMENT = "EMPTY_DOCUMENT"
    INVALID_DOCUMENT = "INVALID_DOCUMENT"
    DOCUMENT_TOO_COMPLEX = "DOCUMENT_TOO_COMPLEX"


EMPTY_DOCUMENT: Final = ParseErrorCode.EMPTY_DOCUMENT
INVALID_DOCUMENT: Final = ParseErrorCode.INVALID_DOCUMENT
DOCUMENT_TOO_COMPLEX: Final = ParseErrorCode.DOCUMENT_TOO_COMPLEX
PARSE_ERROR_CODES: Final = frozenset(ParseErrorCode)


class DocumentParseError(Exception):
    """``code`` : ``EMPTY_DOCUMENT`` (aucun contenu), ``INVALID_DOCUMENT``
    (contenu illisible ou format non pris en charge) ou ``DOCUMENT_TOO_COMPLEX``
    (imbrication trop profonde pour le parseur)."""

    def __init__(self, code: ParseErrorCode, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
