"""Backoff et classification des erreurs : fonctions pures (S0 §7, §5, §3).

API figée :

- ``compute_backoff(attempts, *, base, cap, uniform)`` -> ``timedelta`` ;
  ``uniform(a, b)`` est l'aléa injecté (``random.uniform`` en production).
- ``classify_error(error, *, attempts, max_attempts)`` -> ``FailureDecision``.
- ``decide_recovery(*, attempts, max_attempts)`` -> ``FailureDecision``
  (module neutre ``adapters/failure_policy.py``, partagé avec l'adapter SQL de
  ``recover``).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from knowledge_base.adapters.controller.tasks.retry_policy import (
    StaleAttempt,
    TerminalWorkError,
    TransientWorkError,
    classify_error,
    compute_backoff,
)
from knowledge_base.adapters.failure_policy import decide_recovery

BASE = timedelta(seconds=10)
CAP = timedelta(seconds=900)


def lowest(low: float, _high: float) -> float:
    return low


def highest(_low: float, high: float) -> float:
    return high


def backoff_seconds(attempts: int, uniform: object = highest) -> float:
    delay = compute_backoff(
        attempts,
        base=BASE,
        cap=CAP,
        uniform=uniform,  # type: ignore[arg-type]
    )
    return delay.total_seconds()


# --- backoff ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attempts", "expected_ceiling"),
    [(1, 10), (2, 20), (3, 40), (4, 80), (7, 640)],
)
def test_backoff_doubles_with_each_attempt(
    attempts: int, expected_ceiling: float
) -> None:
    # Aléa au maximum : le délai vaut d = base * 2^(attempts - 1).
    assert backoff_seconds(attempts, highest) == expected_ceiling


@pytest.mark.parametrize("attempts", [8, 9, 50, 5_000])
def test_backoff_is_capped_even_for_huge_attempt_counts(attempts: int) -> None:
    # 10 * 2^7 = 1280 > 900 : plafonné. 5 000 ne doit pas déborder.
    assert backoff_seconds(attempts, highest) == 900
    assert backoff_seconds(attempts, lowest) == 450


@pytest.mark.parametrize("attempts", [1, 3, 8])
def test_backoff_equal_jitter_lower_bound_is_half_of_the_delay(attempts: int) -> None:
    ceiling = backoff_seconds(attempts, highest)
    assert backoff_seconds(attempts, lowest) == ceiling / 2


def test_backoff_draws_the_jitter_in_zero_to_half_of_the_delay() -> None:
    draws: list[tuple[float, float]] = []

    def spy(low: float, high: float) -> float:
        draws.append((low, high))
        return 0.0

    delay = compute_backoff(3, base=BASE, cap=CAP, uniform=spy)

    # d = 40 s : délai = d/2 + U(0, d/2), l'aléa est tiré sur [0, 20].
    assert draws == [(0.0, 20.0)]
    assert delay == timedelta(seconds=20)


def test_backoff_adds_the_random_draw_to_half_of_the_delay() -> None:
    delay = compute_backoff(1, base=BASE, cap=CAP, uniform=lambda _a, _b: 2.5)
    assert delay == timedelta(seconds=7.5)


def test_backoff_uses_the_configured_base_and_cap() -> None:
    delay = compute_backoff(
        2,
        base=timedelta(seconds=3),
        cap=timedelta(seconds=5),
        uniform=highest,
    )
    assert delay == timedelta(seconds=5)  # min(5, 3 * 2) = 5


@pytest.mark.parametrize("attempts", [0, -1])
def test_backoff_rejects_attempts_below_one(attempts: int) -> None:
    with pytest.raises(ValueError, match="attempts"):
        compute_backoff(attempts, base=BASE, cap=CAP, uniform=highest)


# --- classification des erreurs -----------------------------------------------


def test_error_classification_terminal_error_fails_with_its_own_code() -> None:
    decision = classify_error(
        TerminalWorkError("EMPTY_DOCUMENT", "le fichier est vide"),
        attempts=1,
        max_attempts=5,
    )

    assert decision.action == "fail"
    assert decision.failure is not None
    assert decision.failure.code == "EMPTY_DOCUMENT"
    assert "le fichier est vide" in decision.failure.message


def test_error_classification_transient_error_is_retried() -> None:
    decision = classify_error(
        TransientWorkError("qdrant indisponible"), attempts=1, max_attempts=5
    )

    assert decision.action == "retry"
    assert decision.failure is not None
    assert decision.failure.code == "TRANSIENT"
    assert "qdrant indisponible" in decision.failure.message


def test_error_classification_unknown_exception_is_retried_with_its_type() -> None:
    decision = classify_error(KeyError("boom"), attempts=2, max_attempts=5)

    assert decision.action == "retry"
    assert decision.failure is not None
    assert decision.failure.code == "UNEXPECTED_ERROR"
    assert "KeyError" in decision.failure.message


@pytest.mark.parametrize(
    "error",
    [TransientWorkError("x"), RuntimeError("x")],
    ids=["transient", "unknown"],
)
def test_error_classification_last_attempt_fails_instead_of_retrying(
    error: Exception,
) -> None:
    decision = classify_error(error, attempts=5, max_attempts=5)

    assert decision.action == "fail"
    assert decision.failure is not None


def test_error_classification_one_attempt_before_the_last_still_retries() -> None:
    decision = classify_error(RuntimeError("x"), attempts=4, max_attempts=5)
    assert decision.action == "retry"


def test_error_classification_terminal_error_fails_even_on_the_first_attempt() -> None:
    decision = classify_error(TerminalWorkError("X"), attempts=1, max_attempts=5)
    assert decision.action == "fail"


def test_error_classification_stale_attempt_changes_nothing() -> None:
    decision = classify_error(StaleAttempt(), attempts=1, max_attempts=5)

    assert decision.action == "ignore"
    assert decision.failure is None


def test_terminal_work_error_exposes_code_and_message() -> None:
    error = TerminalWorkError("CODE", "message")
    assert error.code == "CODE"
    assert "message" in str(error)


# --- décision de reprise d'un lease expiré --------------------------------------


@pytest.mark.parametrize(("attempts", "max_attempts"), [(1, 5), (4, 5), (1, 2)])
def test_recover_decision_expired_lease_with_attempts_left_goes_back_to_pending(
    attempts: int, max_attempts: int
) -> None:
    decision = decide_recovery(attempts=attempts, max_attempts=max_attempts)

    assert decision.action == "retry"
    assert decision.failure is not None
    assert decision.failure.code == "LEASE_EXPIRED"


@pytest.mark.parametrize(("attempts", "max_attempts"), [(5, 5), (6, 5), (1, 1)])
def test_recover_decision_expired_lease_without_attempts_left_fails(
    attempts: int, max_attempts: int
) -> None:
    decision = decide_recovery(attempts=attempts, max_attempts=max_attempts)

    assert decision.action == "fail"
    assert decision.failure is not None
    assert decision.failure.code == "LEASE_EXPIRED_MAX_ATTEMPTS"
