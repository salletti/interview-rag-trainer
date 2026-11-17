"""Ressources de l'API pour Knowledge Base, créées par ``runtime`` au
démarrage de l'application (``lifespan``)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_base.frameworks.configuration.settings import KnowledgeBaseSettings


@dataclass(frozen=True)
class KnowledgeBaseResources:
    session_factory: async_sessionmaker[AsyncSession]
    settings: KnowledgeBaseSettings
