"""``ProvisionSearchIndex`` initial (S4) : création et activation du premier
index sur un corpus vide (knowledge-base.md §5.4, §13 ; ADR 0011 §4).

Commande idempotente, exécutée au démarrage de la stack (``python -m
runtime.provision``) : si le corpus a déjà un index actif, elle le rend sans
rien écrire ni appeler Qdrant.

Ordre des effets : la collection est créée **avant** la transaction qui
enregistre l'index. Un index visible en SQL a donc toujours sa collection ;
l'inverse (collection orpheline après un échec du commit) est inoffensif.

Activer sur un corpus vide satisfait I11 trivialement : sans index, aucune
version n'a pu être publiée (I9). La migration vers un autre profil (index
``BUILDING`` et backfill) est celle de S9.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from knowledge_base.domain.entities.knowledge_corpus import (
    KnowledgeCorpus,
    SearchIndex,
)
from knowledge_base.domain.providers.indexing_transaction import IndexingUnitOfWork
from knowledge_base.domain.providers.knowledge_corpus_repository import (
    CorpusAlreadyExistsError,
)
from knowledge_base.domain.providers.vector_index import VectorIndex
from knowledge_base.use_cases.provision_search_index.provision_search_index_command import (  # noqa: E501
    ProvisionSearchIndex,
)

logger = logging.getLogger(__name__)


class ProvisionSearchIndexHandler:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], IndexingUnitOfWork],
        vector_index: VectorIndex,
        id_factory: Callable[[], UUID],
        clock: Callable[[], datetime],
    ) -> None:
        self._unit_of_work = unit_of_work
        self._vector_index = vector_index
        self._id_factory = id_factory
        self._clock = clock

    async def handle(self, command: ProvisionSearchIndex) -> UUID:
        """Identifiant de l'index actif du corpus."""
        async with self._unit_of_work() as uow:
            active = _active(await uow.corpus.get_for_update())
        if active is not None:
            _warn_if_other_profile(active, command)
            return active.index_id

        index_id = self._id_factory()
        await self._vector_index.ensure_collection(index_id, command.profile)
        try:
            return await self._record(index_id, command)
        except CorpusAlreadyExistsError:
            logger.info("corpus créé par un provisionnement concurrent : relecture")
            async with self._unit_of_work() as uow:
                winner = _active(await uow.corpus.get_for_update())
            if winner is None:
                raise
            return winner.index_id

    async def _record(self, index_id: UUID, command: ProvisionSearchIndex) -> UUID:
        async with self._unit_of_work() as uow:
            corpus = await uow.corpus.get_for_update()
            active = _active(corpus)
            if active is not None:
                # Un autre processus a fini entre les deux transactions ; la
                # collection créée ci-dessus reste orpheline et vide.
                return active.index_id
            now = self._clock()
            created = corpus is None
            corpus = corpus or KnowledgeCorpus.empty()
            corpus.provision_index(
                index_id=index_id,
                profile=command.profile,
                chunking_policy=command.chunking_policy,
                now=now,
            )
            corpus.activate_index(index_id, now=now, coverage_complete=True)
            if created:
                await uow.corpus.add(corpus)
            else:
                await uow.corpus.save(corpus)
            await uow.commit()
        logger.info(
            "index initial %s provisionné et activé (profil %s@%s, politique %s)",
            index_id,
            command.profile.model,
            command.profile.revision,
            command.chunking_policy.identity,
        )
        return index_id


def _active(corpus: KnowledgeCorpus | None) -> SearchIndex | None:
    return None if corpus is None else corpus.active_index()


def _warn_if_other_profile(active: SearchIndex, command: ProvisionSearchIndex) -> None:
    if (active.profile, active.chunking_policy) != (
        command.profile,
        command.chunking_policy,
    ):
        logger.warning(
            "l'index actif %s a un autre profil ou une autre politique que la "
            "configuration (%s, %s) : il reste actif ; changer de profil passe "
            "par une migration blue/green (S9)",
            active.index_id,
            command.profile.model,
            command.chunking_policy.identity,
        )
