"""Bounded Context Knowledge Base (ADR 0007).

Surface publique vue par ``runtime``, réexportée depuis
``adapters/controller/dependencies`` (005). Le reste du package est interne au
contexte.
"""

from knowledge_base.adapters.controller.dependencies import (
    IndexOperationRefusedError,
    KnowledgeBaseResources,
    KnowledgeBaseSettings,
    KnowledgeBaseWorkerResources,
    WorkHandler,
    abandon_index_migration,
    activate_search_index,
    build_knowledge_search,
    build_work_handlers,
    migration_target_names,
    provision_search_index,
    register,
    register_worker,
    start_index_migration,
)

__all__ = [
    "IndexOperationRefusedError",
    "KnowledgeBaseResources",
    "KnowledgeBaseSettings",
    "KnowledgeBaseWorkerResources",
    "WorkHandler",
    "abandon_index_migration",
    "activate_search_index",
    "build_knowledge_search",
    "build_work_handlers",
    "migration_target_names",
    "provision_search_index",
    "register",
    "register_worker",
    "start_index_migration",
]
