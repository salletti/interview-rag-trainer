"""``ActivateSearchIndex`` : bascule d'un index ``BUILDING`` en ``ACTIVE`` sous
la barrière du corpus (S9 ; knowledge-base.md §4.1 I9-I11, §5.4, §13 ;
ADR 0011 §3, §5).

UNE transaction, ouverte par la lecture du corpus ``FOR UPDATE`` : aucune
extraction ni publication (``FOR SHARE``) ne passe entre la preuve et la
bascule ; une publication en cours est attendue, puis vue.

1. L'index doit exister (``UnknownIndexError``) et être ``BUILDING``
   (``InvalidIndexTransitionError``).
2. Couverture (I11), CALCULÉE : chaque version ``PUBLISHED`` d'un document
   ``ACTIVE`` a une ``Indexation COMPLETED`` dans l'index candidat, sinon
   ``IncompleteCoverageError`` (agrégat). Les versions ``EXTRACTED`` ne sont
   pas exigées : publiées plus tard, elles le seront d'après l'index actif
   (I9) ; une version déjà ``COMPLETED`` dans le candidat mais jamais publiée
   l'est par la réconciliation après la bascule (publication perdue, S8).
3. Aucune synchronisation de visibilité en attente pour un document publié
   (``SynchronizationPendingError``) : le backfill d'une version déjà publiée
   écrit ses points invisibles et replanifie sa synchronisation (§11, S8) ;
   activer avant elle viderait la recherche de ce document.
4. Bascule par l'agrégat (I10 : l'ancien index devient ``RETIRED``) et item
   ``drop_index_collection`` de l'ancien index, disponible après
   ``drop_delay`` : une recherche commencée avant le commit peut encore
   interroger l'ancienne collection. Aucun appel Qdrant ici.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID

from knowledge_base.domain.entities.indexation import IndexationStatus
from knowledge_base.domain.entities.knowledge_corpus import (
    IndexStatus,
    KnowledgeCorpus,
)
from knowledge_base.domain.entities.knowledge_document import VersionStatus
from knowledge_base.domain.exceptions.corpus_errors import (
    InvalidIndexTransitionError,
    UnknownIndexError,
)
from knowledge_base.domain.providers.index_migration import (
    IndexableVersion,
    MigrationUnitOfWork,
)
from knowledge_base.use_cases.activate_search_index.activate_search_index_command import (  # noqa: E501
    ActivateSearchIndex,
)
from knowledge_base.use_cases.activate_search_index.errors import (
    SynchronizationPendingError,
)
from knowledge_base.use_cases.indexing_steps import schedule_collection_drop

logger = logging.getLogger(__name__)


class ActivateSearchIndexHandler:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], MigrationUnitOfWork],
        clock: Callable[[], datetime],
        drop_delay: timedelta = timedelta(0),
    ) -> None:
        """``drop_delay`` : délai avant la suppression de la collection de
        l'ancien index ; la commande d'exploitation passe
        ``KB_INDEX_DROP_DELAY_SECONDS``."""
        if drop_delay < timedelta(0):
            raise ValueError(f"drop_delay négatif : {drop_delay}")
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._drop_delay = drop_delay

    async def handle(self, command: ActivateSearchIndex) -> None:
        async with self._unit_of_work() as uow:
            corpus = await uow.corpus.get_for_update()
            _ensure_building(corpus, command.index_id)
            assert corpus is not None  # _ensure_building l'a vérifié
            published = [
                v
                for v in await uow.versions.list_indexable_versions()
                if v.version_status is VersionStatus.PUBLISHED
            ]
            uncovered = await _uncovered(uow, published, command.index_id)
            previous = corpus.active_index_id
            now = self._clock()
            if not uncovered:
                awaiting = await uow.versions.documents_awaiting_visibility()
                pending = awaiting & {v.document_id for v in published}
                if pending:
                    raise SynchronizationPendingError(command.index_id, pending)
            else:
                logger.info(
                    "activation de l'index %s refusée : %d version(s) publiée(s) "
                    "non COMPLETED dans l'index, dont %s",
                    command.index_id,
                    len(uncovered),
                    uncovered[0],
                )
            corpus.activate_index(
                command.index_id, now=now, coverage_complete=not uncovered
            )
            await uow.corpus.save(corpus)
            if previous is not None:
                await schedule_collection_drop(
                    uow.work_queue, previous, delay=self._drop_delay
                )
            await uow.commit()
        logger.info(
            "index %s activé (%d version(s) publiée(s) couvertes) ; ancien index "
            "%s RETIRED, collection supprimée dans %s",
            command.index_id,
            len(published),
            previous,
            self._drop_delay,
        )


def _ensure_building(corpus: KnowledgeCorpus | None, index_id: UUID) -> None:
    if corpus is None:
        raise UnknownIndexError(index_id)
    index = corpus.get_index(index_id)
    if index.status is not IndexStatus.BUILDING:
        raise InvalidIndexTransitionError(index_id, index.status, "activation")


async def _uncovered(
    uow: MigrationUnitOfWork, published: list[IndexableVersion], index_id: UUID
) -> list[UUID]:
    """Versions publiées sans ``Indexation COMPLETED`` dans l'index (I11)."""
    missing: list[UUID] = []
    for version in published:
        indexation = await uow.indexations.get(version.version_id, index_id)
        if indexation is None or indexation.status is not IndexationStatus.COMPLETED:
            missing.append(version.version_id)
    return missing
