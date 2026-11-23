"""``DropIndexCollection`` : suppression de la collection Qdrant d'un index
retiré (S9 ; knowledge-base.md §13 : « l'ancien index est retiré, puis sa
collection supprimée par un item de maintenance » ; ADR 0011 §4).

L'index doit être ``RETIRED`` (lecture brève sous ``FOR SHARE``, fermée avant
l'appel Qdrant) : ``RETIRED`` est terminal, la décision ne peut plus être
contredite. Un index vivant n'est jamais supprimé (``IndexStillLiveError``).
La ligne ``RETIRED`` reste dans le corpus : ses indexations y renvoient.
Rejouable : une collection déjà supprimée n'est pas une erreur ; une panne de
Qdrant remonte et l'item est rejoué.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from knowledge_base.domain.entities.knowledge_corpus import IndexStatus
from knowledge_base.domain.exceptions.corpus_errors import UnknownIndexError
from knowledge_base.domain.providers.index_collections import IndexCollections
from knowledge_base.domain.providers.unit_of_work import UnitOfWork
from knowledge_base.use_cases.drop_index_collection.drop_index_collection_command import (  # noqa: E501
    DropIndexCollection,
)
from knowledge_base.use_cases.drop_index_collection.errors import (
    IndexStillLiveError,
)

logger = logging.getLogger(__name__)


class DropIndexCollectionHandler:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], UnitOfWork],
        collections: IndexCollections,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._collections = collections

    async def handle(self, command: DropIndexCollection) -> None:
        async with self._unit_of_work() as uow:
            corpus = await uow.corpus.get_shared()
        if corpus is None:
            raise UnknownIndexError(command.index_id)
        index = corpus.get_index(command.index_id)
        if index.status is not IndexStatus.RETIRED:
            raise IndexStillLiveError(index.index_id, index.status)
        await self._collections.delete_collection(index.index_id)
        logger.info(
            "collection de l'index retiré %s supprimée (retiré le %s)",
            index.index_id,
            index.retired_at,
        )
