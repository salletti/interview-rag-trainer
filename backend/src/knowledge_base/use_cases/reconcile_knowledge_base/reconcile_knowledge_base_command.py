from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# Kind du work item qui exécute un cycle (S8). Un cycle FAILED n'est ni listé
# par ListFailures ni rejoué par RetryIngestion : le cycle suivant le remplace.
RECONCILE_KNOWLEDGE_BASE: Final = "reconcile_knowledge_base"


@dataclass(frozen=True)
class ReconcileKnowledgeBase:
    """Un cycle complet de réconciliation ; ses réglages sont ceux du handler
    (``KnowledgeBaseSettings``), pas de la commande."""
