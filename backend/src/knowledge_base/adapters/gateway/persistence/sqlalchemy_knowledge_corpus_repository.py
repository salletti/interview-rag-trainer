"""Dépôt SQLAlchemy de ``KnowledgeCorpus`` : lignes ``knowledge_corpus`` et
``search_indexes`` (knowledge-base.md §3.2, §13).

- ``get_shared`` / ``get_for_update`` verrouillent la **ligne du corpus**
  (``FOR SHARE`` / ``FOR UPDATE``) jusqu'à la fin de la transaction : c'est la
  barrière blue/green d'ADR 0011 §5. Les lignes d'index sont lues sans verrou :
  elles ne changent que sous ``FOR UPDATE`` du corpus.
- ``add`` : ``INSERT … ON CONFLICT DO NOTHING`` ; un corpus déjà créé par une
  transaction concurrente lève ``CorpusAlreadyExistsError`` sans annuler la
  transaction.
- ``save`` : verrou optimiste sur ``version``, puis écriture des index (les
  index non actifs d'abord : l'index unique partiel « un seul ACTIVE » n'est
  pas différable).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Row, Select, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_base.domain.entities.knowledge_corpus import (
    CORPUS_ID,
    IndexStatus,
    KnowledgeCorpus,
    SearchIndex,
)
from knowledge_base.domain.providers.knowledge_corpus_repository import (
    CorpusAlreadyExistsError,
    CorpusModifiedConcurrentlyError,
)
from knowledge_base.domain.value_objects.chunking_policy import (
    ChunkingPolicy,
    CodeFallback,
)
from knowledge_base.domain.value_objects.embedding_profile import (
    Distance,
    EmbeddingProfile,
)
from knowledge_base.frameworks.sqlalchemy.tables import knowledge_corpus, search_indexes

_c = knowledge_corpus.c
_i = search_indexes.c


class SqlAlchemyKnowledgeCorpusRepository:
    """Travaille dans la session de l'Unit of Work ; ne valide ni n'annule
    jamais la transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_shared(self) -> KnowledgeCorpus | None:
        return await self._load(self._corpus_row().with_for_update(read=True))

    async def get_for_update(self) -> KnowledgeCorpus | None:
        return await self._load(self._corpus_row().with_for_update())

    async def add(self, corpus: KnowledgeCorpus) -> None:
        inserted = await self._session.scalar(
            insert(knowledge_corpus)
            .values(
                corpus_id=corpus.corpus_id,
                active_index_id=corpus.active_index_id,
                version=corpus.version,
            )
            .on_conflict_do_nothing(index_elements=[_c.corpus_id])
            .returning(_c.corpus_id)
        )
        if inserted is None:
            raise CorpusAlreadyExistsError
        await self._write_indexes(corpus)

    async def save(self, corpus: KnowledgeCorpus) -> None:
        saved = await self._session.scalar(
            update(knowledge_corpus)
            .where(_c.corpus_id == corpus.corpus_id, _c.version == corpus.version)
            .values(active_index_id=corpus.active_index_id, version=_c.version + 1)
            .returning(_c.version)
        )
        if saved is None:
            raise CorpusModifiedConcurrentlyError(corpus.corpus_id)
        await self._write_indexes(corpus)

    @staticmethod
    def _corpus_row() -> Select[Any]:
        return select(knowledge_corpus).where(_c.corpus_id == CORPUS_ID)

    async def _load(self, statement: Select[Any]) -> KnowledgeCorpus | None:
        row = (await self._session.execute(statement)).one_or_none()
        if row is None:
            return None
        index_rows = (
            await self._session.execute(
                select(search_indexes)
                .where(_i.corpus_id == row.corpus_id)
                .order_by(_i.created_at, _i.index_id)
            )
        ).all()
        return KnowledgeCorpus.restore(
            corpus_id=row.corpus_id,
            active_index_id=row.active_index_id,
            version=row.version,
            indexes=[_to_index(r) for r in index_rows],
        )

    async def _write_indexes(self, corpus: KnowledgeCorpus) -> None:
        ordered = sorted(corpus.indexes, key=lambda i: i.status is IndexStatus.ACTIVE)
        for index in ordered:
            statement = insert(search_indexes).values(
                corpus_id=corpus.corpus_id, **_index_values(index)
            )
            await self._session.execute(
                statement.on_conflict_do_update(
                    index_elements=[_i.index_id],
                    set_={
                        "status": statement.excluded.status,
                        "activated_at": statement.excluded.activated_at,
                        "retired_at": statement.excluded.retired_at,
                    },
                )
            )


def _index_values(index: SearchIndex) -> dict[str, Any]:
    return {
        "index_id": index.index_id,
        "status": index.status.value,
        "embedding_profile": profile_document(index.profile),
        "chunking_policy": policy_document(index.chunking_policy),
        "created_at": index.created_at,
        "activated_at": index.activated_at,
        "retired_at": index.retired_at,
    }


def profile_document(profile: EmbeddingProfile) -> dict[str, Any]:
    return {
        "model": profile.model,
        "revision": profile.revision,
        "dimension": profile.dimension,
        "distance": profile.distance.value,
        "normalize": profile.normalize,
        "query_prefix": profile.query_prefix,
        "passage_prefix": profile.passage_prefix,
        "max_tokens": profile.max_tokens,
        "tokenizer": profile.tokenizer,
    }


def policy_document(policy: ChunkingPolicy) -> dict[str, Any]:
    return {
        "version": policy.version,
        "tokenizer": policy.tokenizer,
        "max_tokens": policy.max_tokens,
        "overlap_tokens": policy.overlap_tokens,
        "fallbacks": [f.value for f in policy.fallbacks],
    }


def _to_index(row: Row[Any]) -> SearchIndex:
    profile = row.embedding_profile
    policy = row.chunking_policy
    return SearchIndex(
        index_id=row.index_id,
        profile=EmbeddingProfile(
            model=profile["model"],
            revision=profile["revision"],
            dimension=profile["dimension"],
            distance=Distance(profile["distance"]),
            normalize=profile["normalize"],
            query_prefix=profile["query_prefix"],
            passage_prefix=profile["passage_prefix"],
            max_tokens=profile["max_tokens"],
            tokenizer=profile["tokenizer"],
        ),
        chunking_policy=ChunkingPolicy(
            version=policy["version"],
            tokenizer=policy["tokenizer"],
            max_tokens=policy["max_tokens"],
            overlap_tokens=policy["overlap_tokens"],
            fallbacks=tuple(CodeFallback(f) for f in policy["fallbacks"]),
        ),
        status=IndexStatus(row.status),
        created_at=row.created_at,
        activated_at=row.activated_at,
        retired_at=row.retired_at,
    )
