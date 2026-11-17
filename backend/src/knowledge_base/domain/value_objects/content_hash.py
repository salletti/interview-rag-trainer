"""Empreinte du contenu original d'une version (knowledge-base.md §3.5, I3)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

_SHA256_HEX: Final = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class ContentHash:
    """SHA-256 hexadécimal minuscule : une seule écriture par contenu, pour
    que l'égalité par valeur suffise à détecter un doublon."""

    value: str

    def __post_init__(self) -> None:
        if not _SHA256_HEX.fullmatch(self.value):
            raise ValueError(
                f"empreinte SHA-256 invalide (64 hexadécimaux minuscules "
                f"attendus) : {self.value!r}"
            )
