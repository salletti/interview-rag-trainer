from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReconcileKnowledgeBase:
    """Un cycle complet de réconciliation ; ses réglages sont ceux du handler
    (``KnowledgeBaseSettings``), pas de la commande."""
