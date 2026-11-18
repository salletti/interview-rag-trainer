"""Chemin de sections d'une unité ou d'une citation (knowledge-base.md §3.5)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SectionPath:
    """Titres englobants, du plus général au plus précis ; vide à la racine
    du document."""

    parts: tuple[str, ...] = ()

    def __str__(self) -> str:
        return " > ".join(self.parts)
