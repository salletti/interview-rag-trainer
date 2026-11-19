"""Décisions d'échec d'une tentative : fonctions pures, sans I/O.

Module neutre des adapters : la reprise d'un lease expiré est décidée ici et
appliquée par l'adapter SQL (``recover_expired``), sans que le gateway dépende
du controller. ``retry_policy`` (controller) s'appuie sur le même type.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from knowledge_base.domain.providers.work_queue import (
    LEASE_EXPIRED,
    LEASE_EXPIRED_MAX_ATTEMPTS,
    FailureInfo,
)

FailureAction = Literal["retry", "fail", "ignore"]


@dataclass(frozen=True)
class FailureDecision:
    action: FailureAction
    failure: FailureInfo | None

    def __post_init__(self) -> None:
        if (self.action == "ignore") != (self.failure is None):
            raise ValueError(
                f"une décision {self.action!r} doit avoir une erreur "
                "si et seulement si elle n'est pas 'ignore'"
            )


def has_attempts_left(*, attempts: int, max_attempts: int) -> bool:
    return attempts < max_attempts


def decide_recovery(*, attempts: int, max_attempts: int) -> FailureDecision:
    """Décision pour un lease expiré (transition ``recover`` du §3)."""
    if has_attempts_left(attempts=attempts, max_attempts=max_attempts):
        return FailureDecision(
            "retry",
            FailureInfo(LEASE_EXPIRED, f"lease expiré pendant la tentative {attempts}"),
        )
    return FailureDecision(
        "fail",
        FailureInfo(
            LEASE_EXPIRED_MAX_ATTEMPTS,
            f"lease expiré pendant la dernière tentative ({attempts}/{max_attempts})",
        ),
    )
