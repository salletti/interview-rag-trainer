"""Journal d'une tentative en échec (défaut D5 de la validation globale).

Un échec métier attendu (``TerminalWorkError`` : code du catalogue, ex.
``OCR_REQUIRED``) est un cas normal : une ligne WARNING avec son code, sans
trace, même quand l'erreur d'origine est chaînée. Une erreur inattendue garde
sa trace, en ERROR ; une erreur transitoire déclarée reste un WARNING tracé.

API : ``log_failed_attempt(item, failure, action, error)`` dans
``adapters/controller/tasks/execution.py``.
"""

from __future__ import annotations

import logging
from uuid import UUID

import pytest

from knowledge_base.adapters.controller.tasks.execution import log_failed_attempt
from knowledge_base.adapters.controller.tasks.retry_policy import (
    TerminalWorkError,
    TransientWorkError,
    classify_error,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    ClaimedWorkItem,
)
from knowledge_base.domain.exceptions.parsing_errors import (
    DocumentParseError,
    ParseErrorCode,
)

LOGGER = "knowledge_base.adapters.controller.tasks.execution"
ITEM = ClaimedWorkItem(
    id=UUID(int=7),
    kind="extract_version",
    payload={},
    attempts=1,
    max_attempts=5,
    fencing_token=1,
    correlation_id=None,
)


def chained_ocr_refusal() -> TerminalWorkError:
    """Comme ``ExtractVersionWorkHandler`` : le refus du parseur est chaîné."""
    try:
        try:
            raise DocumentParseError(
                ParseErrorCode.OCR_REQUIRED, "PDF sans couche texte\nOCR désactivé"
            )
        except DocumentParseError as error:
            raise TerminalWorkError(error.code.value, error.message) from error
    except TerminalWorkError as terminal:
        return terminal


def log(error: Exception, caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    decision = classify_error(error, attempts=ITEM.attempts, max_attempts=5)
    assert decision.failure is not None
    with caplog.at_level(logging.INFO, logger=LOGGER):
        log_failed_attempt(ITEM, decision.failure, decision.action, error)
    [record] = caplog.records
    return record


def test_an_expected_terminal_failure_is_one_warning_line_without_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    record = log(chained_ocr_refusal(), caplog)

    assert record.levelno == logging.WARNING
    assert record.exc_info is None
    message = record.getMessage()
    assert "OCR_REQUIRED" in message
    assert str(ITEM.id) in message
    assert "\n" not in message
    assert "\n" not in caplog.text.rstrip("\n")
    assert "Traceback" not in caplog.text


def test_an_unexpected_error_keeps_its_traceback_at_error_level(
    caplog: pytest.LogCaptureFixture,
) -> None:
    try:
        raise RuntimeError("boom inattendu")
    except RuntimeError as error:
        record = log(error, caplog)

    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert "UNEXPECTED_ERROR" in record.getMessage()
    assert "Traceback" in caplog.text


def test_a_declared_transient_error_is_a_traced_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    record = log(TransientWorkError("qdrant indisponible"), caplog)

    assert record.levelno == logging.WARNING
    assert record.exc_info is not None
    assert "TRANSIENT" in record.getMessage()
