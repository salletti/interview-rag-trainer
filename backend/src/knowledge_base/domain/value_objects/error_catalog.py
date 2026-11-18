"""Catalogue unique des codes d'erreur de Knowledge Base (S8 ; knowledge-base.md
§15.4 ; ADR 0004 points 7 et 10 ; O14).

Chaque code écrit dans ``work_items.last_error_code`` ou dans l'échec d'une
version ou d'une indexation est défini ICI, une seule fois ; les autres
modules l'importent ou le réexportent (test d'architecture
``test_reconciliation_structure``). Deux axes, distincts :

- ``automatic_retry`` : la work queue réessaie seule, avec backoff (erreur
  transitoire), jusqu'à la dernière tentative ;
- ``replayable`` : ``RetryIngestion`` peut rouvrir l'item ``FAILED`` une fois
  la cause corrigée hors de l'item (volume restauré, collection recréée,
  service revenu, borne relevée, version corrigée déployée). Un code
  déterministe n'est jamais réessayé seul, mais il peut être rejouable après
  réparation (``BLOB_MISSING``) ; un code qui tient au seul contenu
  (``EMPTY_DOCUMENT``) ne l'est jamais : un nouvel essai échouerait pareil.

Un refus qui dépend d'une borne (``DOCUMENT_TOO_LARGE`` :
``KB_MAX_DOCUMENT_TOKENS`` ; ``TOO_MANY_PAGES`` : limite de pages du parseur)
ou du code déployé (``NOT_FINALIZED``, ``UNKNOWN_KIND``)
est rejouable : le même contenu ne peut pas être réimporté (O2 : contenu
connu -> version existante), le rejeu est la seule voie de reprise une fois
la borne relevée ou le correctif déployé. Toujours sans réessai automatique :
un rejeu sans correction échoue une fois de plus, sans boucle.

Un code inconnu du catalogue (version future, faute de frappe) lève
``UnknownErrorCodeError`` : jamais « rejouable par défaut ».
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

# --- work queue (S0) ----------------------------------------------------------
TRANSIENT: Final = "TRANSIENT"
UNEXPECTED_ERROR: Final = "UNEXPECTED_ERROR"
LEASE_EXPIRED: Final = "LEASE_EXPIRED"
LEASE_EXPIRED_MAX_ATTEMPTS: Final = "LEASE_EXPIRED_MAX_ATTEMPTS"
TERMINAL_HOOK_FAILED: Final = "TERMINAL_HOOK_FAILED"
UNKNOWN_KIND: Final = "UNKNOWN_KIND"
NOT_FINALIZED: Final = "NOT_FINALIZED"
INVALID_PAYLOAD: Final = "INVALID_PAYLOAD"

# --- extraction (S3, S6, S8) ---------------------------------------------------
BLOB_MISSING: Final = "BLOB_MISSING"
EMPTY_DOCUMENT: Final = "EMPTY_DOCUMENT"
INVALID_DOCUMENT: Final = "INVALID_DOCUMENT"
DOCUMENT_TOO_COMPLEX: Final = "DOCUMENT_TOO_COMPLEX"
OCR_REQUIRED: Final = "OCR_REQUIRED"
TOO_MANY_PAGES: Final = "TOO_MANY_PAGES"
CHUNKING_FAILED: Final = "CHUNKING_FAILED"
# Borne de volume de texte extrait, tous formats (O16) : distincte de
# DOCUMENT_TOO_COMPLEX (imbrication, XML décompressé d'un DOCX).
DOCUMENT_TOO_LARGE: Final = "DOCUMENT_TOO_LARGE"

# --- indexation (S4) -----------------------------------------------------------
COLLECTION_MISMATCH: Final = "COLLECTION_MISMATCH"


class UnknownErrorCodeError(KeyError):
    """Code absent du catalogue : ni réessayé, ni rejouable par supposition."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code

    def __str__(self) -> str:
        return f"code d'erreur inconnu du catalogue : {self.code!r}"


@dataclass(frozen=True)
class ErrorSpec:
    code: str
    automatic_retry: bool
    replayable: bool

    def __post_init__(self) -> None:
        # Un code réessayé seul finit FAILED à la dernière tentative sans que
        # sa cause tienne au contenu : il doit pouvoir être rejoué.
        if self.automatic_retry and not self.replayable:
            raise ValueError(
                f"{self.code} : un code réessayé automatiquement est rejouable"
            )


def _spec(code: str, *, automatic_retry: bool, replayable: bool) -> ErrorSpec:
    return ErrorSpec(code, automatic_retry=automatic_retry, replayable=replayable)


ERROR_CATALOG: Final[Mapping[str, ErrorSpec]] = MappingProxyType(
    {
        spec.code: spec
        for spec in (
            # Transitoires : backoff, puis FAILED à la dernière tentative.
            _spec(TRANSIENT, automatic_retry=True, replayable=True),
            _spec(UNEXPECTED_ERROR, automatic_retry=True, replayable=True),
            _spec(LEASE_EXPIRED, automatic_retry=True, replayable=True),
            # Tentatives épuisées ou hook d'échec en erreur : l'infrastructure
            # a manqué, pas le contenu (O14).
            _spec(LEASE_EXPIRED_MAX_ATTEMPTS, automatic_retry=False, replayable=True),
            _spec(TERMINAL_HOOK_FAILED, automatic_retry=False, replayable=True),
            # Déterministes, mais réparables hors de l'item.
            _spec(BLOB_MISSING, automatic_retry=False, replayable=True),
            _spec(COLLECTION_MISMATCH, automatic_retry=False, replayable=True),
            # Bornes (KB_MAX_DOCUMENT_TOKENS ; MAX_PDF_PAGES du parseur) :
            # rejouables une fois la borne relevée (O2 interdit le réimport).
            _spec(TOO_MANY_PAGES, automatic_retry=False, replayable=True),
            _spec(DOCUMENT_TOO_LARGE, automatic_retry=False, replayable=True),
            # Défauts du code déployé : rejouables une fois le correctif
            # déployé (worker qui ne connaît pas le kind, handler fautif).
            _spec(UNKNOWN_KIND, automatic_retry=False, replayable=True),
            _spec(NOT_FINALIZED, automatic_retry=False, replayable=True),
            # Déterministes sur le contenu.
            _spec(EMPTY_DOCUMENT, automatic_retry=False, replayable=False),
            _spec(INVALID_DOCUMENT, automatic_retry=False, replayable=False),
            _spec(DOCUMENT_TOO_COMPLEX, automatic_retry=False, replayable=False),
            _spec(OCR_REQUIRED, automatic_retry=False, replayable=False),
            _spec(CHUNKING_FAILED, automatic_retry=False, replayable=False),
            # Payload illisible : l'item ne désigne rien, un rejeu relirait le
            # même payload.
            _spec(INVALID_PAYLOAD, automatic_retry=False, replayable=False),
        )
    }
)


def spec_of(code: str) -> ErrorSpec:
    try:
        return ERROR_CATALOG[code]
    except KeyError:
        raise UnknownErrorCodeError(code) from None


def is_replayable(code: str) -> bool:
    return spec_of(code).replayable


def retries_automatically(code: str) -> bool:
    return spec_of(code).automatic_retry


def is_known_replayable(code: str | None) -> bool:
    """``is_replayable``, un code absent ou inconnu valant « non rejouable » :
    pour ce qui doit répondre sans lever (liste des échecs, hook d'échec, dont
    une exception ferait boucler l'item)."""
    if code is None:
        return False
    try:
        return is_replayable(code)
    except UnknownErrorCodeError:
        return False
