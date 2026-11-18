"""Port de comptage des tokens du profil d'embeddings (ADR 0009 §3).

Le budget d'un chunk n'a de sens que mesuré avec le tokenizer du modèle qui
l'encodera : l'implémentation est fournie par l'adapter d'embeddings.
"""

from __future__ import annotations

from typing import Protocol


class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...
