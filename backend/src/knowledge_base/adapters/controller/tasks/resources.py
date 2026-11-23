"""Ressources d'un processus worker pour Knowledge Base (S0 §9).

Créées par ``runtime`` dans chaque processus enfant, après le fork : la boucle
asyncio est persistante et le moteur SQLAlchemy lui est attaché.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_base.adapters.controller.tasks.registry import (
    HandlerRegistry,
    WorkHandler,
)


@dataclass(frozen=True)
class KnowledgeBaseWorkerResources:
    session_factory: async_sessionmaker[AsyncSession]
    loop: asyncio.AbstractEventLoop
    handlers: tuple[WorkHandler, ...] = ()
    # Construit une fois : un double enregistrement échoue dès la création
    # des ressources, pas à la première tâche.
    registry: HandlerRegistry = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "registry", HandlerRegistry(self.handlers))
