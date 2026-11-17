"""Bounded Context Knowledge Base (ADR 0007).

Surface publique vue par ``runtime``, réexportée depuis
``adapters/controller/dependencies`` (005). Le reste du package est interne au
contexte.
"""

from knowledge_base.adapters.controller.dependencies import (
    KnowledgeBaseSettings,
    KnowledgeBaseWorkerResources,
    WorkHandler,
    register_worker,
)

__all__ = [
    "KnowledgeBaseSettings",
    "KnowledgeBaseWorkerResources",
    "WorkHandler",
    "register_worker",
]
