"""Cause métier de l'échec d'extraction d'une version (knowledge-base.md §3.5).

Distinct de ``FailureInfo`` (``domain/providers/work_queue.py``), qui décrit
l'échec d'une *tentative* de work item : ``FailureReason`` est l'état durable
de la version et dit si ``RetryIngestion`` a un sens (``retryable``).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FailureReason:
    code: str
    message: str
    retryable: bool

    def __post_init__(self) -> None:
        if not self.code.strip():
            raise ValueError("code d'échec ne peut pas être vide")
