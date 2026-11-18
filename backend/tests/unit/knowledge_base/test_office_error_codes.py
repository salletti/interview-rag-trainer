"""Codes d'erreur de parsing de S6 (ADR 0008, point 5 ; roadmap S6).

``ParseErrorCode`` est une liste fermée : le handler d'extraction tient chacun
de ses codes pour non rejouable (``NOT_RETRYABLE_CODES``) ; le crochet d'échec
de ``test_extract_version_handler`` couvre déjà chaque membre de l'énumération
(``retryable`` faux). S6 ajoute deux codes :

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


@pytest.mark.parametrize("name", ["OCR_REQUIRED", "TOO_MANY_PAGES"])
def test_the_s6_codes_are_closed_list_members_named_after_their_value(
    name: str,
) -> None:
    code = ParseErrorCode[name]

    assert code.value == name
    assert code in PARSE_ERROR_CODES
    assert getattr(parsing_errors, name) is code


@pytest.mark.parametrize("name", ["OCR_REQUIRED", "TOO_MANY_PAGES"])
def test_the_s6_codes_are_never_retried(name: str) -> None:
    assert ParseErrorCode[name].value in NOT_RETRYABLE_CODES


def test_a_parse_error_carries_its_s6_code_and_message() -> None:
    error = DocumentParseError(ParseErrorCode.OCR_REQUIRED, "scan.pdf : image seule")

    assert error.code == "OCR_REQUIRED"
    assert error.message == "scan.pdf : image seule"
    assert str(error) == "OCR_REQUIRED: scan.pdf : image seule"
