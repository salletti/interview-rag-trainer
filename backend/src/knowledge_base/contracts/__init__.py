"""Contrat public de Knowledge Base (knowledge-base.md §14 ; ADR 0007) : la
seule partie du contexte qu'un autre contexte peut importer. Bibliothèque
standard uniquement."""

from knowledge_base.contracts.providers import (
    KnowledgeSearch,
    KnowledgeSearchUnavailableError,
    UnavailabilityReason,
)

__all__ = ["KnowledgeSearch", "KnowledgeSearchUnavailableError", "UnavailabilityReason"]
