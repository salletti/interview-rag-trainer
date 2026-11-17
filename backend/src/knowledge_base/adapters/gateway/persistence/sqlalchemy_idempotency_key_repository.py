"""Dépôt SQLAlchemy des clés d'idempotence de l'import."""

from __future__ import annotations

from typing import Final

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_base.adapters.gateway.persistence.constraint_violations import (
    violated_constraint,
)
from knowledge_base.domain.providers.idempotency_key_repository import (
    IdempotencyKeyTakenError,
    IdempotencyRecord,
)
from knowledge_base.frameworks.sqlalchemy.tables import idempotency_keys

KEY_PRIMARY_KEY: Final = "pk_idempotency_keys"

_k = idempotency_keys.c


class SqlAlchemyIdempotencyKeyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, key: str) -> IdempotencyRecord | None:
        row = (
            await self._session.execute(
                select(
                    _k.key,
                    _k.fingerprint,
                    _k.document_id,
                    _k.version_id,
                    _k.number,
                    _k.created,
                ).where(_k.key == key)
            )
        ).one_or_none()
        if row is None:
            return None
        return IdempotencyRecord(
            key=row.key,
            fingerprint=row.fingerprint,
            document_id=row.document_id,
            version_id=row.version_id,
            number=row.number,
            created=row.created,
        )

    async def add(self, record: IdempotencyRecord) -> None:
        try:
            await self._session.execute(
                insert(idempotency_keys).values(
                    key=record.key,
                    fingerprint=record.fingerprint,
                    document_id=record.document_id,
                    version_id=record.version_id,
                    number=record.number,
                    created=record.created,
                )
            )
        except IntegrityError as error:
            if violated_constraint(error) == KEY_PRIMARY_KEY:
                raise IdempotencyKeyTakenError(record.key) from error
            raise
