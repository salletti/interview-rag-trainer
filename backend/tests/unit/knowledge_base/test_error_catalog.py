"""Catalogue unique des codes d'erreur (S8 ; knowledge-base.md §15, O14 ;
ADR 0004 points 7 et 10).

API figée (``knowledge_base.domain.value_objects.error_catalog``) :

- ``ErrorSpec(code, automatic_retry, replayable)`` : deux axes distincts.
  ``automatic_retry`` : la work queue réessaie seule, avec backoff (erreur
  transitoire). ``replayable`` : ``RetryIngestion`` peut rouvrir l'item
  ``FAILED`` après correction de la cause (un humain ou une réparation
  extérieure intervient). Un code déterministe n'est jamais réessayé seul ;
  il peut néanmoins être rejouable après réparation (``BLOB_MISSING`` : le
  fichier est restauré ; ``COLLECTION_MISMATCH`` : la collection est
  recréée). C'est ce qui lève l'incohérence de S3 (« terminal mais
  ``retryable=True`` ») : ``BLOB_MISSING`` est terminal pour la work queue ET
  rejouable pour ``RetryIngestion``, les deux sont dits par le catalogue.
- ``ERROR_CATALOG: Mapping[str, ErrorSpec]``, ``spec_of(code)``,
  ``is_replayable(code)``, ``retries_automatically(code)`` ; un code inconnu
  lève ``UnknownErrorCodeError`` (jamais « rejouable par défaut »).
"""

from __future__ import annotations

import importlib

import pytest

from knowledge_base.adapters.controller.tasks.retry_policy import (
    TerminalWorkError,
    TransientWorkError,
    classify_error,
)
from knowledge_base.adapters.failure_policy import decide_recovery
from knowledge_base.domain.exceptions.parsing_errors import PARSE_ERROR_CODES
from knowledge_base.domain.value_objects.error_catalog import (
    ERROR_CATALOG,
    UnknownErrorCodeError,
    is_replayable,
    retries_automatically,
    spec_of,
)
from tests.unit.knowledge_base.s8_support import TEXT_VOLUME_CODE

# code -> (réessayé automatiquement, rejouable par RetryIngestion)
EXPECTED: dict[str, tuple[bool, bool]] = {
    # transitoires : backoff, puis FAILED à la dernière tentative
    "TRANSIENT": (True, True),
    "UNEXPECTED_ERROR": (True, True),
    "LEASE_EXPIRED": (True, True),
    # épuisement des tentatives ou hook en échec : infrastructure, rejouable
    "LEASE_EXPIRED_MAX_ATTEMPTS": (False, True),
    "TERMINAL_HOOK_FAILED": (False, True),
    # déterministes mais réparables hors de l'item
    "BLOB_MISSING": (False, True),
    "COLLECTION_MISMATCH": (False, True),
    # déterministes sur le contenu : un nouvel essai échouerait pareillement
    "EMPTY_DOCUMENT": (False, False),
    "INVALID_DOCUMENT": (False, False),
    "DOCUMENT_TOO_COMPLEX": (False, False),
    "OCR_REQUIRED": (False, False),
    "TOO_MANY_PAGES": (False, False),
    "CHUNKING_FAILED": (False, False),
    TEXT_VOLUME_CODE: (False, False),
    # défauts de programmation ou de déploiement : rien à rouvrir
    "INVALID_PAYLOAD": (False, False),
    "UNKNOWN_KIND": (False, False),
    "NOT_FINALIZED": (False, False),
}


def test_the_catalog_lists_exactly_the_known_codes() -> None:
    assert set(ERROR_CATALOG) == set(EXPECTED)


@pytest.mark.parametrize(("code", "expected"), list(EXPECTED.items()))
def test_every_code_is_classified_on_both_axes(
    code: str, expected: tuple[bool, bool]
) -> None:
    automatic, replayable = expected

    assert retries_automatically(code) is automatic
    assert is_replayable(code) is replayable
    spec = spec_of(code)
    assert (spec.code, spec.automatic_retry, spec.replayable) == (
        code,
        automatic,
        replayable,
    )


def test_blob_missing_is_terminal_for_the_queue_and_replayable_after_restoration() -> (
    None
):
    assert not retries_automatically("BLOB_MISSING")
    assert is_replayable("BLOB_MISSING")


def test_an_unknown_code_is_refused_rather_than_guessed() -> None:
    with pytest.raises(UnknownErrorCodeError):
        is_replayable("SOMETHING_ELSE")
    with pytest.raises(UnknownErrorCodeError):
        retries_automatically("SOMETHING_ELSE")


def test_a_code_retried_automatically_is_always_replayable_once_exhausted() -> None:
    assert all(
        spec.replayable for spec in ERROR_CATALOG.values() if spec.automatic_retry
    )


# (module, nom) : les noms publics existants gardent leur valeur ; ils peuvent
# être réexportés depuis le catalogue, mais ne doivent pas disparaître.
OLD_NAMES = [
    ("knowledge_base.domain.providers.work_queue", "TRANSIENT"),
    ("knowledge_base.domain.providers.work_queue", "UNEXPECTED_ERROR"),
    ("knowledge_base.domain.providers.work_queue", "UNKNOWN_KIND"),
    ("knowledge_base.domain.providers.work_queue", "NOT_FINALIZED"),
    ("knowledge_base.domain.providers.work_queue", "TERMINAL_HOOK_FAILED"),
    ("knowledge_base.domain.providers.work_queue", "LEASE_EXPIRED"),
    ("knowledge_base.domain.providers.work_queue", "LEASE_EXPIRED_MAX_ATTEMPTS"),
    (
        "knowledge_base.adapters.controller.tasks.extract_version_handler",
        "BLOB_MISSING",
    ),
    (
        "knowledge_base.adapters.controller.tasks.extract_version_handler",
        "INVALID_PAYLOAD",
    ),
    (
        "knowledge_base.adapters.controller.tasks.extract_version_handler",
        "CHUNKING_FAILED",
    ),
    ("knowledge_base.use_cases.complete_indexation.errors", "COLLECTION_MISMATCH"),
]


@pytest.mark.parametrize(("module", "name"), OLD_NAMES)
def test_every_code_defined_elsewhere_is_in_the_catalog(module: str, name: str) -> None:
    value = getattr(importlib.import_module(module), name)

    assert value == name
    assert value in ERROR_CATALOG


def test_every_parse_error_is_a_non_replayable_content_failure() -> None:
    for code in PARSE_ERROR_CODES:
        assert not retries_automatically(code.value)
        assert not is_replayable(code.value)


# --- cohérence avec la classification réelle de la work queue ------------------------


def test_the_queue_retries_exactly_the_codes_the_catalog_says_it_retries() -> None:
    transient = classify_error(TransientWorkError("x"), attempts=1, max_attempts=5)
    unexpected = classify_error(ValueError("x"), attempts=1, max_attempts=5)
    leased = decide_recovery(attempts=1, max_attempts=5)
    leased_last = decide_recovery(attempts=5, max_attempts=5)
    produced = {
        decision.failure.code: decision.action == "retry"
        for decision in (transient, unexpected, leased, leased_last)
        if decision.failure is not None
    }

    assert produced == {
        "TRANSIENT": True,
        "UNEXPECTED_ERROR": True,
        "LEASE_EXPIRED": True,
        "LEASE_EXPIRED_MAX_ATTEMPTS": False,
    }
    for code, retried in produced.items():
        assert retries_automatically(code) is retried, code


@pytest.mark.parametrize(
    "code", [c for c, (automatic, _) in EXPECTED.items() if not automatic]
)
def test_a_terminal_error_never_retries_whatever_attempts_remain(code: str) -> None:
    decision = classify_error(
        TerminalWorkError(code, "diagnostic"), attempts=1, max_attempts=5
    )

    assert decision.action == "fail"
    assert decision.failure is not None
    assert decision.failure.code == code
