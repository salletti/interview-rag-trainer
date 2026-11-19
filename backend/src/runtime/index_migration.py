"""Commande d'exploitation : migration blue/green de l'index de recherche de
Knowledge Base (S9 ; knowledge-base.md §13 ; ADR 0011 §4).

Lancement (dans un conteneur de l'image backend, avec l'environnement de la
stack ; ``make kb-migration-start target=<nom>``, ``make
kb-migration-activate index=<index_id>`` et ``make kb-migration-abandon
index=<index_id>``) :

- ``python -m runtime.index_migration start <nom>`` : nouvel index
  ``BUILDING`` pour la cible ``<nom>`` du catalogue, sa collection Qdrant et
  le backfill du corpus (work items exécutés par ``worker_ingestion``) ;
  rejouable. Affiche l'identifiant de l'index.
- ``python -m runtime.index_migration activate <index_id>`` : bascule de la
  recherche, refusée tant que la couverture est incomplète ou qu'une
  synchronisation de visibilité est en attente ou en échec ; à relancer plus
  tard.
- ``python -m runtime.index_migration abandon <index_id>`` : abandon d'une
  migration (bloquée ou non voulue) : l'index ``BUILDING`` passe ``RETIRED``,
  son travail d'indexation est annulé et sa collection supprimée après
  ``KB_INDEX_DROP_DELAY_SECONDS`` ; l'index actif reste servi. Refusé pour un
  index qui n'est pas ``BUILDING``.

Pas de route HTTP : une bascule est une opération rare, volontaire et lourde.
Un refus (cible inconnue, migration en cours, couverture incomplète…) sort en
erreur avec son message ; rien n'est écrit.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence
from uuid import UUID

import knowledge_base
from runtime.database import create_engine, create_session_factory
from runtime.logging_config import configure_logging
from runtime.settings import get_settings
from runtime.vector_store import create_qdrant_client

logger = logging.getLogger(__name__)


async def start(target_name: str) -> UUID:
    settings = get_settings()
    engine = create_engine(settings)
    qdrant = create_qdrant_client(settings)
    try:
        index_id = await knowledge_base.start_index_migration(
            create_session_factory(engine), qdrant, target_name
        )
    finally:
        await qdrant.close()
        await engine.dispose()
    logger.info("migration vers %s : index BUILDING %s", target_name, index_id)
    return index_id


async def activate(index_id: UUID) -> None:
    engine = create_engine(get_settings())
    try:
        await knowledge_base.activate_search_index(
            create_session_factory(engine), index_id
        )
    finally:
        await engine.dispose()
    logger.info("index %s actif", index_id)


async def abandon(index_id: UUID) -> None:
    engine = create_engine(get_settings())
    try:
        await knowledge_base.abandon_index_migration(
            create_session_factory(engine), index_id
        )
    finally:
        await engine.dispose()
    logger.info("migration vers l'index %s abandonnée", index_id)


def parser() -> argparse.ArgumentParser:
    commands = argparse.ArgumentParser(
        prog="python -m runtime.index_migration",
        description="Migration blue/green de l'index de recherche de Knowledge Base",
    )
    actions = commands.add_subparsers(dest="action", required=True)
    started = actions.add_parser("start", help="démarre une migration (backfill)")
    started.add_argument(
        "target", choices=knowledge_base.migration_target_names(), help="cible"
    )
    activated = actions.add_parser("activate", help="bascule la recherche")
    activated.add_argument("index_id", type=UUID, help="index BUILDING à activer")
    abandoned = actions.add_parser("abandon", help="abandonne une migration")
    abandoned.add_argument("index_id", type=UUID, help="index BUILDING à abandonner")
    return commands


def main(argv: Sequence[str] | None = None) -> None:
    arguments = parser().parse_args(argv)
    configure_logging(get_settings().log_level)
    if arguments.action == "start":
        print(asyncio.run(start(arguments.target)))  # noqa: T201 - sortie de la commande
    elif arguments.action == "activate":
        asyncio.run(activate(arguments.index_id))
    else:
        asyncio.run(abandon(arguments.index_id))


if __name__ == "__main__":
    main()
