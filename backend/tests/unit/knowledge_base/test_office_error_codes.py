"""Codes d'erreur de parsing de S6 (ADR 0008, point 5 ; roadmap S6).

``ParseErrorCode`` est une liste fermée ; aucun de ses codes n'est réessayé
automatiquement (terminal). Les codes de contenu sont non rejouables
(``NOT_RETRYABLE_CODES``) ; ``TOO_MANY_PAGES`` dépend de la borne de pages
configurée : rejouable une fois la borne relevée (revue S8, I1 : O2 interdit
de réimporter le même contenu, le rejeu est la seule reprise). S6 ajoute deux
codes :

- ``OCR_REQUIRED`` : PDF sans couche texte (scan) ; l'OCR est désactivé en V1 ;
- ``TOO_MANY_PAGES`` : PDF de plus de 300 pages (ADR 0012, point 4).
"""

from __future__ import annotations

import pytest

from knowledge_base.adapters.controller.tasks.extract_version_handler import (
    NOT_RETRYABLE_CODES,
)
from knowledge_base.domain.exceptions import parsing_errors
from knowledge_base.domain.exceptions.parsing_errors import (
    PARSE_ERROR_CODES,
    DocumentParseError,
    ParseErrorCode,
)
from knowledge_base.domain.value_objects.error_catalog import (
    is_replayable,
    retries_automatically,
)


@pytest.mark.parametrize("name", ["OCR_REQUIRED", "TOO_MANY_PAGES"])
def test_the_s6_codes_are_closed_list_members_named_after_their_value(
    name: str,
) -> None:
    code = ParseErrorCode[name]

    assert code.value == name
    assert code in PARSE_ERROR_CODES
    assert getattr(parsing_errors, name) is code


@pytest.mark.parametrize("name", ["OCR_REQUIRED", "TOO_MANY_PAGES"])
def test_the_s6_codes_are_never_retried_automatically(name: str) -> None:
    assert not retries_automatically(ParseErrorCode[name].value)


def test_a_scan_is_never_replayable() -> None:
    assert ParseErrorCode.OCR_REQUIRED.value in NOT_RETRYABLE_CODES


def test_a_page_limit_is_replayable_once_raised() -> None:
    assert ParseErrorCode.TOO_MANY_PAGES.value not in NOT_RETRYABLE_CODES
    assert is_replayable(ParseErrorCode.TOO_MANY_PAGES.value)


def test_a_parse_error_carries_its_s6_code_and_message() -> None:
    error = DocumentParseError(ParseErrorCode.OCR_REQUIRED, "scan.pdf : image seule")

    assert error.code == "OCR_REQUIRED"
    assert error.message == "scan.pdf : image seule"
    assert str(error) == "OCR_REQUIRED: scan.pdf : image seule"
