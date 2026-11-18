"""Refus de ``RetryIngestion`` : levés avant toute écriture."""

from __future__ import annotations

from uuid import UUID


class RetryRefusedError(Exception):
    def __init__(self, item_id: UUID, message: str) -> None:
        super().__init__(message)
        self.item_id = item_id


class FailureNotFoundError(RetryRefusedError):
    def __init__(self, item_id: UUID) -> None:
        super().__init__(item_id, f"aucun item de travail {item_id}")


class ItemNotFailedError(RetryRefusedError):
    """L'item n'est pas ``FAILED`` (en attente, réussi, annulé, ou déjà
    rouvert par un rejeu concurrent)."""

    def __init__(self, item_id: UUID, status: str) -> None:
        super().__init__(
            item_id, f"l'item {item_id} est {status}, seul un item FAILED se rejoue"
        )
        self.status = status


class NotReplayableError(RetryRefusedError):
    """Le catalogue des erreurs dit l'échec non rejouable (contenu, payload
    illisible) ou ne connaît pas son code."""

    def __init__(self, item_id: UUID, code: str, reason: str | None = None) -> None:
        super().__init__(
            item_id,
            f"l'échec {code or '(sans code)'} de l'item {item_id} n'est pas "
            f"rejouable : {reason or 'un nouvel essai échouerait pareillement'}",
        )
        self.code = code


class SupersededCycleError(NotReplayableError):
    """Cycle de réconciliation ``FAILED`` : jamais rejoué, quel que soit son
    code ; le cycle du créneau suivant le remplace."""

    def __init__(self, item_id: UUID, code: str) -> None:
        super().__init__(
            item_id,
            code,
            "un cycle de réconciliation ne se rejoue pas, le suivant le remplace",
        )


class ObsoleteWorkError(RetryRefusedError):
    """Le travail n'a plus d'objet : version supplantée, document retiré,
    index retiré, indexation annulée."""

    def __init__(self, item_id: UUID, reason: str) -> None:
        super().__init__(item_id, f"l'item {item_id} n'a plus d'objet : {reason}")
        self.reason = reason
