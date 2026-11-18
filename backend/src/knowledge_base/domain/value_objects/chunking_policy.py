"""Règles de découpage versionnées (knowledge-base.md §2 ; ADR 0009 §3-5, §7).

``version`` fait partie de l'identité des unités et chunks produits : changer
une règle, le budget ou l'ordre des fallbacks impose une nouvelle version.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CodeFallback(StrEnum):
    """Frontières successives de découpe d'un bloc de code trop grand
    (ADR 0009 §4)."""

    BLANK_LINES = "BLANK_LINES"
    DEFINITIONS = "DEFINITIONS"
    LINES = "LINES"


DEFAULT_CODE_FALLBACKS = (
    CodeFallback.BLANK_LINES,
    CodeFallback.DEFINITIONS,
    CodeFallback.LINES,
)


@dataclass(frozen=True)
class ChunkingPolicy:
    version: str
    max_tokens: int
    overlap_tokens: int = 0
    fallbacks: tuple[CodeFallback, ...] = DEFAULT_CODE_FALLBACKS

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("version de politique vide")
        if self.max_tokens <= 0:
            raise ValueError(f"max_tokens doit être positif : {self.max_tokens}")
        if not 0 <= self.overlap_tokens < self.max_tokens:
            raise ValueError(
                f"overlap_tokens hors de [0, max_tokens) : {self.overlap_tokens}"
            )
        if len(set(self.fallbacks)) != len(self.fallbacks):
            raise ValueError(f"fallback répété : {self.fallbacks}")
