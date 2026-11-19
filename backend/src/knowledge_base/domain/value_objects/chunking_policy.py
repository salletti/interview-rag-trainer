"""Règles de découpage versionnées (knowledge-base.md §2 ; ADR 0009 §3-5, §7).

``identity`` (``version`` + ``tokenizer``) fait partie de l'identité des
unités et chunks produits, et c'est elle qui est persistée comme
``chunking_policy_version`` : changer une règle, le budget ou l'ordre des
fallbacks impose une nouvelle ``version`` ; changer de compteur de tokens
impose un autre ``tokenizer``, car le même budget ne découpe plus pareil.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


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

IDENTITY_SEPARATOR: Final = "+"


@dataclass(frozen=True)
class ChunkingPolicy:
    version: str
    tokenizer: str
    max_tokens: int
    overlap_tokens: int = 0
    fallbacks: tuple[CodeFallback, ...] = DEFAULT_CODE_FALLBACKS

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("version de politique vide")
        if not self.tokenizer.strip():
            raise ValueError("tokenizer de politique vide")
        # Sans séparateur dans le tokenizer, ``identity`` se relit sans
        # ambiguïté : deux couples distincts ne donnent jamais la même chaîne.
        if IDENTITY_SEPARATOR in self.tokenizer:
            raise ValueError(
                f"tokenizer contenant {IDENTITY_SEPARATOR!r} : {self.tokenizer!r}"
            )
        if self.max_tokens <= 0:
            raise ValueError(f"max_tokens doit être positif : {self.max_tokens}")
        if not 0 <= self.overlap_tokens < self.max_tokens:
            raise ValueError(
                f"overlap_tokens hors de [0, max_tokens) : {self.overlap_tokens}"
            )
        if len(set(self.fallbacks)) != len(self.fallbacks):
            raise ValueError(f"fallback répété : {self.fallbacks}")

    @property
    def identity(self) -> str:
        """``version+tokenizer`` : ce qui distingue un lot d'unités et de
        chunks, dans les identifiants, le ``chunk_hash`` et la persistance."""
        return f"{self.version}{IDENTITY_SEPARATOR}{self.tokenizer}"
