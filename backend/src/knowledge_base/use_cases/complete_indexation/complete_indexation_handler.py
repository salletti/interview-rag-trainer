"""``CompleteIndexation`` : écrire les points d'une version dans un index,
les vérifier, puis finaliser l'``Indexation`` et tenter la publication
(knowledge-base.md §6, §7.3, §8, §9 ; I8, I9, I14).

Coupé en deux pour la work queue (S0 §5) :

- ``prepare`` : lecture brève (transaction fermée avant tout effet externe),
  puis vérification de la collection (une fois), embeddings et upserts
  ``visible=false`` par lots (``wait=true``), relecture par identifiants et
  égalité exacte avec le manifest attendu. N'écrit rien dans PostgreSQL.
- ``apply`` : dans la transaction de finalisation, revérification métier puis
  ``COMPLETED`` (I8) et tentative de publication ; une publication refusée
  (indexation d'un index ``BUILDING``, I5…) laisse l'indexation ``COMPLETED``.

Effets externes rejouables : points ``uuid5(index_id, chunk_id)`` écrasés à
l'identique, jamais ajoutés (§7.3).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import batched
from typing import Final
from uuid import UUID

from knowledge_base.domain.entities.indexation import Indexation, IndexationStatus
from knowledge_base.domain.entities.knowledge_corpus import (
    IndexStatus,
    KnowledgeCorpus,
    SearchIndex,
)
from knowledge_base.domain.entities.knowledge_document import (
    DocumentStatus,
    DocumentVersion,
    KnowledgeDocument,
    VersionStatus,
)
from knowledge_base.domain.exceptions.corpus_errors import UnknownIndexError
from knowledge_base.domain.exceptions.document_errors import (
    KnowledgeDocumentError,
    VersionNotFoundError,
)
from knowledge_base.domain.providers.embedder import Embedder
from knowledge_base.domain.providers.indexing_transaction import (
    IndexingTransaction,
    IndexingUnitOfWork,
)
from knowledge_base.domain.providers.vector_index import VectorIndex
from knowledge_base.domain.services.point_id import point_id
from knowledge_base.domain.value_objects.document_chunk import DocumentChunk
from knowledge_base.domain.value_objects.failure_reason import FailureReason
from knowledge_base.domain.value_objects.index_manifest import IndexManifest
from knowledge_base.domain.value_objects.indexed_point import IndexedPoint
from knowledge_base.use_cases.complete_indexation.complete_indexation_command import (
    CompleteIndexation,
)
from knowledge_base.use_cases.complete_indexation.errors import (
    CollectionMismatchError,
    IndexManifestMismatchError,
)
from knowledge_base.use_cases.publish_document_version.errors import (
    PublicationRefusedError,
)
from knowledge_base.use_cases.publish_document_version.publish_document_version_command import (  # noqa: E501
    PublishDocumentVersion,
)
from knowledge_base.use_cases.publish_document_version.publish_document_version_handler import (  # noqa: E501
    PublishDocumentVersionHandler,
)

logger = logging.getLogger(__name__)

# Relecture du manifest : identifiants par requête ``retrieve``.
RETRIEVE_BATCH: Final = 256

_INDEXABLE_VERSIONS: Final = frozenset(
    {VersionStatus.EXTRACTED, VersionStatus.PUBLISHED}
)


@dataclass(frozen=True)
class PreparedIndexation:
    document_id: UUID
    version_id: UUID
    index_id: UUID
    manifest: IndexManifest
    point_count: int


@dataclass(frozen=True)
class _Target:
    """Ce que ``prepare`` doit indexer : la version, son document et l'index."""

    document: KnowledgeDocument
    version: DocumentVersion
    index: SearchIndex
    indexation: Indexation


class CompleteIndexationHandler:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], IndexingUnitOfWork],
        embedder: Embedder,
        vector_index: VectorIndex,
        publisher: PublishDocumentVersionHandler,
        clock: Callable[[], datetime],
        batch_size: int = 32,
    ) -> None:
        if batch_size <= 0:
            raise ValueError(f"batch_size doit être positif : {batch_size}")
        self._unit_of_work = unit_of_work
        self._embedder = embedder
        self._vector_index = vector_index
        self._publisher = publisher
        self._clock = clock
        self._batch_size = batch_size

    # --- hors transaction -------------------------------------------------------

    async def prepare(self, command: CompleteIndexation) -> PreparedIndexation | None:
        """``None`` (aucun appel externe) s'il n'y a rien à indexer. Lève
        ``CollectionMismatchError`` (terminal) ou
        ``IndexManifestMismatchError`` (transitoire)."""
        async with self._unit_of_work() as uow:
            target = _target(
                command,
                await uow.documents.get(command.document_id),
                await uow.corpus.get_shared(),
                await uow.indexations.get(command.version_id, command.index_id),
            )
            chunks = (
                ()
                if target is None
                else await uow.chunks.list_chunks(
                    command.version_id, target.indexation.chunking_policy_version
                )
            )
        if target is None:
            logger.info(
                "indexation de la version %s dans l'index %s ignorée : rien à "
                "indexer (version, document, index ou indexation hors d'état)",
                command.version_id,
                command.index_id,
            )
            return None

        await self._check_collection(target.index)
        for batch in batched(chunks, self._batch_size, strict=False):
            await self._write(target, batch)
        manifest = IndexManifest.for_chunks(target.index.index_id, chunks)
        await self._verify(command, manifest)
        return PreparedIndexation(
            document_id=command.document_id,
            version_id=command.version_id,
            index_id=command.index_id,
            manifest=manifest,
            point_count=len(manifest),
        )

    async def _check_collection(self, index: SearchIndex) -> None:
        if self._embedder.profile != index.profile:
            raise CollectionMismatchError(
                index.index_id,
                f"modèle chargé {self._embedder.profile.model}@"
                f"{self._embedder.profile.revision}, profil de l'index "
                f"{index.profile.model}@{index.profile.revision}",
            )
        if not await self._vector_index.check_collection(index.index_id, index.profile):
            raise CollectionMismatchError(
                index.index_id,
                f"collection absente ou autre que {index.profile.dimension} "
                f"dimensions, distance {index.profile.distance}",
            )

    async def _write(self, target: _Target, batch: Sequence[DocumentChunk]) -> None:
        index = target.index
        vectors = await self._embedder.embed_passages(
            [c.contextual_text for c in batch]
        )
        for vector in vectors:
            if len(vector) != index.profile.dimension:
                raise CollectionMismatchError(
                    index.index_id,
                    f"vecteur de dimension {len(vector)}, profil "
                    f"{index.profile.dimension}",
                )
        document = target.document
        points = [
            IndexedPoint(
                point_id=point_id(index.index_id, chunk.chunk_id),
                vector=tuple(vector),
                chunk_id=chunk.chunk_id,
                unit_id=chunk.unit_id,
                version_id=target.version.version_id,
                document_id=document.document_id,
                chunk_hash=chunk.chunk_hash,
                index_id=index.index_id,
                topics=document.topics,
                kind=document.kind.value,
                language=document.language,
            )
            for chunk, vector in zip(batch, vectors, strict=True)
        ]
        await self._vector_index.upsert(index.index_id, points, wait=True)

    async def _verify(
        self, command: CompleteIndexation, manifest: IndexManifest
    ) -> None:
        """I8 : égalité exacte des couples ``(point_id, chunk_hash)`` relus et
        attendus ; un ``count`` ne suffirait pas (ADR 0010 §3)."""
        expected = dict(manifest.entries)
        found: dict[UUID, str] = {}
        for ids in batched(expected, RETRIEVE_BATCH, strict=False):
            found.update(await self._vector_index.retrieve(command.index_id, ids))
        missing = len(expected.keys() - found.keys())
        different = sum(
            1 for point, digest in found.items() if expected.get(point) != digest
        )
        if missing or different:
            raise IndexManifestMismatchError(
                command.version_id,
                command.index_id,
                missing=missing,
                different=different,
            )

    # --- transaction de finalisation ----------------------------------------------

    async def apply(
        self, transaction: IndexingTransaction, prepared: PreparedIndexation
    ) -> bool:
        """Vrai si l'indexation est passée ``COMPLETED`` ; faux si l'état a
        changé depuis ``prepare`` (elle est alors annulée si la version, le
        document ou l'index ne la justifient plus). Ne valide jamais la
        transaction."""
        command = CompleteIndexation(
            prepared.document_id, prepared.version_id, prepared.index_id
        )
        indexation = await self.cancel_if_obsolete(transaction, command)
        if indexation is None:
            return False
        indexation.complete(
            manifest_digest=prepared.manifest.digest,
            point_count=prepared.point_count,
            completed_at=self._clock(),
        )
        await transaction.indexations.save(indexation)
        logger.info(
            "indexation de la version %s dans l'index %s terminée : %d points, "
            "manifest %s",
            prepared.version_id,
            prepared.index_id,
            prepared.point_count,
            prepared.manifest.digest,
        )
        await self._try_publish(transaction, command)
        return True

    async def cancel_if_obsolete(
        self, transaction: IndexingTransaction, command: CompleteIndexation
    ) -> Indexation | None:
        """L'indexation si elle est toujours à finaliser (``PENDING``, version
        indexable, document actif, index vivant). Sinon ``None`` ; une
        indexation ``PENDING`` qui n'a plus d'objet passe ``CANCELLED`` (§5.3)."""
        indexation = await transaction.indexations.get(
            command.version_id, command.index_id
        )
        if indexation is None or indexation.status is not IndexationStatus.PENDING:
            return None
        document = await transaction.documents.get(command.document_id)
        version = _version(document, command.version_id)
        index = _index(await transaction.corpus.get_shared(), command.index_id)
        if (
            document is None
            or version is None
            or index is None
            or document.status is not DocumentStatus.ACTIVE
            or version.status not in _INDEXABLE_VERSIONS
            or index.status is IndexStatus.RETIRED
        ):
            indexation.cancel()
            await transaction.indexations.save(indexation)
            logger.info(
                "indexation de la version %s dans l'index %s annulée : version, "
                "document ou index hors d'état",
                command.version_id,
                command.index_id,
            )
            return None
        return indexation

    async def _try_publish(
        self, transaction: IndexingTransaction, command: CompleteIndexation
    ) -> None:
        try:
            await self._publisher.apply(
                transaction,
                PublishDocumentVersion(command.document_id, command.version_id),
            )
        except (PublicationRefusedError, KnowledgeDocumentError) as refusal:
            # Les refus sont levés avant toute écriture : la transaction ne
            # contient que l'indexation COMPLETED.
            logger.info("version %s non publiée : %s", command.version_id, refusal)

    async def fail(
        self,
        transaction: IndexingTransaction,
        command: CompleteIndexation,
        reason: FailureReason,
    ) -> None:
        """``FailIndexation`` : ``PENDING`` -> ``FAILED`` ; sans effet sinon
        (lever ferait boucler l'item sur son hook, O14). La version reste
        ``EXTRACTED`` : ses chunks sont valides (§5.2)."""
        indexation = await transaction.indexations.get(
            command.version_id, command.index_id
        )
        if indexation is None or indexation.status is not IndexationStatus.PENDING:
            logger.info(
                "échec %s de l'indexation (%s, %s) non enregistré : absente ou "
                "plus PENDING",
                reason.code,
                command.version_id,
                command.index_id,
            )
            return
        indexation.fail(reason)
        await transaction.indexations.save(indexation)


def _target(
    command: CompleteIndexation,
    document: KnowledgeDocument | None,
    corpus: KnowledgeCorpus | None,
    indexation: Indexation | None,
) -> _Target | None:
    version = _version(document, command.version_id)
    index = _index(corpus, command.index_id)
    if (
        document is None
        or version is None
        or index is None
        or indexation is None
        or document.status is not DocumentStatus.ACTIVE
        or version.status not in _INDEXABLE_VERSIONS
        or indexation.status is not IndexationStatus.PENDING
        or index.status is IndexStatus.RETIRED
    ):
        return None
    return _Target(
        document=document, version=version, index=index, indexation=indexation
    )


def _version(
    document: KnowledgeDocument | None, version_id: UUID
) -> DocumentVersion | None:
    if document is None:
        return None
    try:
        return document.get_version(version_id)
    except VersionNotFoundError:
        return None


def _index(corpus: KnowledgeCorpus | None, index_id: UUID) -> SearchIndex | None:
    if corpus is None:
        return None
    try:
        return corpus.get_index(index_id)
    except UnknownIndexError:
        return None
