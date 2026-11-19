"""``StartIndexMigration`` : provisionnement d'un index ``BUILDING`` à côté de
l'index actif et backfill du corpus (S9 ; knowledge-base.md §5.4, §13 ;
ADR 0011 §4, §5, §7).

Distinct de ``ProvisionSearchIndex`` (S4), commande de démarrage de la stack
qui ne crée que le PREMIER index : un redémarrage avec une autre
configuration ne lance jamais de backfill. Une migration est une décision
d'exploitation (``python -m runtime.index_migration start <cible>``).

1. Lecture du corpus sous ``FOR UPDATE`` (transaction sans écriture) : refus
   sans effet s'il n'y a pas d'index actif, si une migration vers un autre
   couple (profil, politique) est en cours (un seul index ``BUILDING``,
   invariant de l'agrégat qui le refuse de nouveau à l'étape 3) ou si
   le couple est celui de l'index actif ; un index ``BUILDING`` du même couple
   est rendu tel quel (rejeu).
2. Collection Qdrant du nouvel index, HORS transaction et AVANT l'écriture :
   un index visible en SQL a toujours sa collection (comme en S4).
3. UNE transaction, corpus relu ``FOR UPDATE`` (la barrière : aucune
   extraction ni publication ne passe) : index ``BUILDING``, puis, pour chaque
   version ``PUBLISHED`` ou ``EXTRACTED`` d'un document ``ACTIVE``, une
   ``Indexation PENDING`` de la politique du nouvel index et son item :
   ``index_version`` si la version a déjà un lot de cette politique,
   ``rechunk_version`` sinon (O4 : re-parsing de l'original). Les versions
   extraites après le commit sont couvertes par ``RecordExtraction``, qui
   voit l'index ``BUILDING`` sous ``FOR SHARE``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from knowledge_base.domain.entities.indexation import Indexation
from knowledge_base.domain.entities.knowledge_corpus import (
    KnowledgeCorpus,
    SearchIndex,
)
from knowledge_base.domain.exceptions.corpus_errors import IndexAlreadyBuildingError
from knowledge_base.domain.providers.index_migration import (
    IndexableVersion,
    MigrationUnitOfWork,
)
from knowledge_base.domain.providers.vector_index import VectorIndex
from knowledge_base.use_cases.indexing_steps import (
    schedule_indexation,
    schedule_rechunk,
)
from knowledge_base.use_cases.start_index_migration.errors import (
    MigrationInProgressError,
    NoActiveIndexToMigrateError,
    NothingToMigrateError,
)
from knowledge_base.use_cases.start_index_migration.start_index_migration_command import (  # noqa: E501
    StartIndexMigration,
)

logger = logging.getLogger(__name__)


class StartIndexMigrationHandler:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], MigrationUnitOfWork],
        vector_index: VectorIndex,
        id_factory: Callable[[], UUID],
        clock: Callable[[], datetime],
    ) -> None:
        self._unit_of_work = unit_of_work
        self._vector_index = vector_index
        self._id_factory = id_factory
        self._clock = clock

    async def handle(self, command: StartIndexMigration) -> UUID:
        """Identifiant de l'index ``BUILDING`` de la migration."""
        async with self._unit_of_work() as uow:
            building = _building_for(await uow.corpus.get_for_update(), command)
        if building is not None:
            logger.info("migration déjà démarrée vers l'index %s", building)
            return building

        index_id = self._id_factory()
        await self._vector_index.ensure_collection(index_id, command.profile)
        return await self._provision(index_id, command)

    async def _provision(self, index_id: UUID, command: StartIndexMigration) -> UUID:
        async with self._unit_of_work() as uow:
            corpus = await uow.corpus.get_for_update()
            building = _replayed(corpus, command)
            if building is not None:
                # Démarrage concurrent du même couple, terminé entre les deux
                # transactions : la collection créée ci-dessus reste orpheline
                # et vide (inoffensif, comme en S4 ; knowledge-base.md §16).
                return building
            assert corpus is not None  # _replayed l'a vérifié
            try:
                corpus.provision_index(
                    index_id=index_id,
                    profile=command.profile,
                    chunking_policy=command.chunking_policy,
                    now=self._clock(),
                )
            except IndexAlreadyBuildingError as error:
                # Migration rivale d'un autre couple validée entre les deux
                # transactions : la collection créée ci-dessus reste orpheline
                # et vide, comme ci-dessus.
                raise _in_progress(corpus.get_index(error.index_id)) from error
            # L'index doit exister avant ses indexations (clé étrangère).
            await uow.corpus.save(corpus)
            versions = await uow.versions.list_indexable_versions()
            rechunked = 0
            for version in versions:
                rechunked += await _backfill(uow, version, corpus.get_index(index_id))
            await uow.commit()
        logger.info(
            "migration démarrée : index %s BUILDING (profil %s@%s, politique %s), "
            "backfill de %d version(s) dont %d à rechunker",
            index_id,
            command.profile.model,
            command.profile.revision,
            command.chunking_policy.identity,
            len(versions),
            rechunked,
        )
        return index_id


async def _backfill(
    uow: MigrationUnitOfWork, version: IndexableVersion, index: SearchIndex
) -> int:
    """1 si la version doit d'abord être rechunkée, 0 sinon."""
    policy = index.chunking_policy.identity
    await uow.indexations.add(
        Indexation.pending(
            version_id=version.version_id,
            index_id=index.index_id,
            chunking_policy_version=policy,
        )
    )
    has_batch = policy in version.policy_versions
    schedule = schedule_indexation if has_batch else schedule_rechunk
    await schedule(
        uow.work_queue, version.document_id, version.version_id, index.index_id
    )
    return 0 if has_batch else 1


def _building_for(
    corpus: KnowledgeCorpus | None, command: StartIndexMigration
) -> UUID | None:
    """Lecture sans effet : comme ``_replayed``, et refus d'emblée d'un autre
    couple en construction, pour ne créer aucune collection (l'agrégat le
    refuse de toute façon dans la transaction d'écriture)."""
    replayed = _replayed(corpus, command)
    building = None if corpus is None else corpus.building_index()
    if replayed is None and building is not None:
        raise _in_progress(building)
    return replayed


def _replayed(
    corpus: KnowledgeCorpus | None, command: StartIndexMigration
) -> UUID | None:
    """L'index ``BUILDING`` du couple demandé s'il existe déjà ; ``None``
    sinon ; refus s'il n'y a pas d'index actif, ou si le couple est celui de
    l'index actif alors que rien n'est en construction."""
    active = None if corpus is None else corpus.active_index()
    if corpus is None or active is None:
        raise NoActiveIndexToMigrateError
    wanted = (command.profile, command.chunking_policy)
    building = corpus.building_index()
    if building is not None:
        same = (building.profile, building.chunking_policy) == wanted
        return building.index_id if same else None
    if (active.profile, active.chunking_policy) == wanted:
        raise NothingToMigrateError(active.index_id)
    return None


def _in_progress(building: SearchIndex) -> MigrationInProgressError:
    return MigrationInProgressError(
        building.index_id, building.profile, building.chunking_policy
    )
