"""Échecs déterministes de l'extraction d'un original (ADR 0008).

Un nouvel essai sur le même contenu échouerait pareillement : le handler de
travail en fait un échec terminal, sans retry.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from knowledge_base.domain.value_objects import error_catalog


class ParseErrorCode(StrEnum):
    """Liste fermée des échecs du parseur ; leurs valeurs sont celles du
    catalogue des erreurs (S8), qui les dit toutes non rejouables."""

    EMPTY_DOCUMENT = error_catalog.EMPTY_DOCUMENT
    INVALID_DOCUMENT = error_catalog.INVALID_DOCUMENT
    DOCUMENT_TOO_COMPLEX = error_catalog.DOCUMENT_TOO_COMPLEX
    OCR_REQUIRED = error_catalog.OCR_REQUIRED
    TOO_MANY_PAGES = error_catalog.TOO_MANY_PAGES


EMPTY_DOCUMENT: Final = ParseErrorCode.EMPTY_DOCUMENT
INVALID_DOCUMENT: Final = ParseErrorCode.INVALID_DOCUMENT
DOCUMENT_TOO_COMPLEX: Final = ParseErrorCode.DOCUMENT_TOO_COMPLEX
OCR_REQUIRED: Final = ParseErrorCode.OCR_REQUIRED
TOO_MANY_PAGES: Final = ParseErrorCode.TOO_MANY_PAGES
PARSE_ERROR_CODES: Final = frozenset(ParseErrorCode)


class DocumentParseError(Exception):
    """``code`` : ``EMPTY_DOCUMENT`` (aucun contenu), ``INVALID_DOCUMENT``
    (contenu illisible, chiffré ou format non pris en charge),
    ``DOCUMENT_TOO_COMPLEX`` (imbrication trop profonde pour le parseur, DOCX
    au-delà de sa limite de XML décompressé),
    ``OCR_REQUIRED`` (PDF sans couche texte : l'OCR est désactivé en V1) ou
    ``TOO_MANY_PAGES`` (PDF au-delà de la limite de pages, ADR 0012)."""

    def __init__(self, code: ParseErrorCode, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
