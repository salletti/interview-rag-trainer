"""Commande de démarrage : provisionne l'index initial de Knowledge Base
(knowledge-base.md §5.4, §13 ; S4).

Lancement : ``python -m runtime.provision`` (service ``kb_provision`` de
``compose.yaml``, après ``migrate`` et avant l'API et les workers).
Idempotente : un corpus qui a déjà un index actif est laissé tel quel. Code de
sortie non nul si PostgreSQL ou Qdrant sont indisponibles : la stack ne
démarre pas sans index, sinon les extractions ne créeraient aucune
indexation.
"""

from __future__ import annotations

import asyncio
import logging

import knowledge_base
from runtime.database import create_engine, create_session_factory
from runtime.logging_config import configure_logging
from runtime.settings import get_settings
from runtime.vector_store import create_qdrant_client

logger = logging.getLogger(__name__)


async def provision() -> None:
    settings = get_settings()
    engine = create_engine(settings)
    qdrant = create_qdrant_client(settings)
    try:
        index_id = await knowledge_base.provision_search_index(
            create_session_factory(engine), qdrant
        )
        logger.info("index actif de Knowledge Base : %s", index_id)
    finally:
        await qdrant.close()
        await engine.dispose()


def main() -> None:
    configure_logging(get_settings().log_level)
    asyncio.run(provision())


if __name__ == "__main__":
    main()
