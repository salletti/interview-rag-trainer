"""Backoff et classification des erreurs : fonctions pures (S0 §5, §7).

L'aléa est injecté (``random.uniform`` en production) : aucune horloge, aucun
état, aucune I/O.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from knowledge_base.adapters.failure_policy import FailureDecision, has_attempts_left
from knowledge_base.domain.providers.work_queue import (
    TRANSIENT,
    UNEXPECTED_ERROR,
    FailureInfo,
)
from knowledge_base.domain.value_objects.error_catalog import spec_of

Uniform = Callable[[float, float], float]


class TerminalWorkError(Exception):
    """Erreur déterministe : une nouvelle tentative échouerait pareillement.

    ``code`` appartient au catalogue des erreurs (S8) : un code inconnu est
    une erreur de programmation, levée ici (``UnknownErrorCodeError``) plutôt
    qu'écrite en base où ``RetryIngestion`` et la liste des échecs le
    tiendraient pour non rejouable sans rien dire."""

    def __init__(self, code: str, message: str = "") -> None:
        spec_of(code)
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


class TransientWorkError(Exception):
    """Erreur passagère (réseau, service indisponible) : nouvelle tentative."""


class StaleAttempt(Exception):  # noqa: N818 — nom de la spécification (S0 §5)
    """La tentative n'est plus la tentative courante (token périmé, item
    annulé ou repris) : elle s'arrête sans aucune transition."""

    def __init__(self, message: str = "tentative périmée") -> None:
        super().__init__(message)


def compute_backoff(
    attempts: int, *, base: timedelta, cap: timedelta, uniform: Uniform
) -> timedelta:
    """``d = min(cap, base * 2^(attempts - 1))``, puis ``d/2 + U(0, d/2)``
    (*equal jitter*)."""
    if attempts < 1:
        raise ValueError(f"attempts doit être >= 1, reçu {attempts}")
    cap_seconds = cap.total_seconds()
    ceiling = base.total_seconds()
    # Doublement borné plutôt que 2**n : aucun débordement pour un grand n.
    for _ in range(attempts - 1):
        if ceiling >= cap_seconds:
            break
        ceiling *= 2
    ceiling = min(ceiling, cap_seconds)
    half = ceiling / 2
    return timedelta(seconds=half + uniform(0.0, half))


def classify_error(
    error: BaseException, *, attempts: int, max_attempts: int
) -> FailureDecision:
    """Terminal → ``fail`` ; transitoire ou inattendue → ``retry`` tant qu'il
    reste des tentatives, ``fail`` à la dernière ; ``StaleAttempt`` →
    ``ignore``."""
    if isinstance(error, StaleAttempt):
        return FailureDecision("ignore", None)
    if isinstance(error, TerminalWorkError):
        return FailureDecision("fail", FailureInfo(error.code, error.message))
    if isinstance(error, TransientWorkError):
        failure = FailureInfo(TRANSIENT, str(error))
    else:
        failure = FailureInfo(UNEXPECTED_ERROR, f"{type(error).__name__}: {error}")
    retry = has_attempts_left(attempts=attempts, max_attempts=max_attempts)
    return FailureDecision("retry" if retry else "fail", failure)
