"""``AbandonIndexMigration`` : abandon d'une migration blue/green, la seule
issue d'une migration bloquée (revue S9 ; knowledge-base.md §5.4
``BUILDING --> RETIRED``, §13 ; ADR 0011 §4).

UNE transaction, ouverte par la lecture du corpus ``FOR UPDATE`` (la barrière
de §13 : aucune extraction ni publication ne lit le corpus entre le retrait et
l'annulation du travail de l'index) ; aucun appel Qdrant.

1. L'agrégat retire l'index (``retire_index``) : inconnu
   (``UnknownIndexError``) ou non ``BUILDING`` (``InvalidIndexTransitionError``,
   dont un second abandon) est refusé sans aucune écriture.
2. Pour chaque indexation de l'index : items ``index_version`` et
   ``rechunk_version`` annulés, générations comprises (le fencing token est
   incrémenté : une tentative en cours ne finalise plus) ; indexation
   ``PENDING`` ou ``FAILED`` -> ``CANCELLED`` (une ``COMPLETED`` reste un
   fait, §5.3). Annulations conditionnelles, donc idempotentes.
   Les items ``sync_visibility`` ne visent aucun index (payload : document et
   révision) : ils convergent chaque index vivant au moment de leur
   exécution, ignorent donc l'index retiré et restent nécessaires à l'index
   actif ; ils ne sont pas annulés.
3. ``drop_index_collection`` de l'index, disponible après ``drop_delay`` (une
   tentative d'indexation zombie peut encore écrire dans la collection ; ses
   points ne sont plus lus par personne).

Après le commit, plus rien ne vise l'index : ``RecordExtraction`` ne
l'alimente plus (index non vivant), la réconciliation ne le couvre plus, un
rejeu d'un échec de cet index est refusé (``OBSOLETE_WORK``) ; une autre
migration peut démarrer.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Final

from knowledge_base.domain.entities.indexation import IndexationStatus
from knowledge_base.domain.exceptions.corpus_errors import UnknownIndexError
from knowledge_base.domain.providers.index_migration import MigrationUnitOfWork
from knowledge_base.use_cases.abandon_index_migration.abandon_index_migration_command import (  # noqa: E501
    AbandonIndexMigration,
)
from knowledge_base.use_cases.indexing_steps import (
    cancel_index_work,
    schedule_collection_drop,
)

logger = logging.getLogger(__name__)

_CANCELLABLE: Final = frozenset({IndexationStatus.PENDING, IndexationStatus.FAILED})


class AbandonIndexMigrationHandler:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], MigrationUnitOfWork],
        clock: Callable[[], datetime],
        drop_delay: timedelta,
    ) -> None:
        """``drop_delay`` : délai avant la suppression de la collection de
        l'index abandonné (``KB_INDEX_DROP_DELAY_SECONDS`` en production)."""
        if drop_delay < timedelta(0):
            raise ValueError(f"drop_delay négatif : {drop_delay}")
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._drop_delay = drop_delay

    async def handle(self, command: AbandonIndexMigration) -> None:
        index_id = command.index_id
        async with self._unit_of_work() as uow:
            corpus = await uow.corpus.get_for_update()
            if corpus is None:
                raise UnknownIndexError(index_id)
            corpus.retire_index(index_id, now=self._clock())
            await uow.corpus.save(corpus)
            statuses = await uow.versions.indexation_statuses(index_id)
            cancelled = 0
            for version_id, status in statuses.items():
                await cancel_index_work(uow.work_queue, version_id, index_id)
                if status not in _CANCELLABLE:
                    continue
                indexation = await uow.indexations.get(version_id, index_id)
                assert indexation is not None  # lue dans la même transaction
                indexation.cancel()
                await uow.indexations.save(indexation)
                cancelled += 1
            await schedule_collection_drop(
                uow.work_queue, index_id, delay=self._drop_delay
            )
            await uow.commit()
        logger.info(
            "migration vers l'index %s abandonnée : index RETIRED, %d indexation(s) "
            "annulée(s) sur %d, collection supprimée dans %s",
            index_id,
            cancelled,
            len(statuses),
            self._drop_delay,
        )
