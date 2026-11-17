"""Transitions SQL de ``knowledge_base.work_items`` (S0 §3).

Toutes les dates viennent de PostgreSQL (``now()``), jamais de l'horloge du
worker. Une transition qui ne modifie aucune ligne n'est pas une erreur : elle
signifie « déjà traité, annulé ou périmé » et se traduit par ``False`` ou
``None``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Update, and_, case, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_base.adapters.failure_policy import decide_recovery
from knowledge_base.domain.providers.work_queue import FailureInfo
from knowledge_base.frameworks.sqlalchemy.tables import work_items

PENDING = "PENDING"
RUNNING = "RUNNING"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"

_c = work_items.c

# Effacement du lease à la sortie de RUNNING.
_RELEASE_LEASE: Mapping[str, Any] = {"lease_owner": None, "lease_expires_at": None}


@dataclass(frozen=True)
class ClaimedWorkItem:
    """Tentative en cours : ``fencing_token`` est son identité."""

    id: UUID
    kind: str
    payload: Mapping[str, str]
    attempts: int
    max_attempts: int
    fencing_token: int
    correlation_id: UUID | None


@dataclass(frozen=True)
class QueueCapacity:
    """Une file de travail, les ``kind`` qu'elle sert et son plafond en vol
    (S0 §8.2 : le plafond est porté par la file, pas par le ``kind``)."""

    queue: str
    kinds: frozenset[str]
    max_in_flight: int


def _error_columns(error: FailureInfo) -> dict[str, str]:
    return {"last_error_code": error.code, "last_error_message": error.message}


class SqlAlchemyWorkQueue:
    """Implémentation du port ``WorkQueue`` dans la session du handler métier.
    N'ouvre, ne valide ni n'annule jamais la transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(
        self,
        kind: str,
        dedup_key: str,
        payload: Mapping[str, str] | None = None,
        correlation_id: UUID | None = None,
        *,
        max_attempts: int | None = None,
    ) -> UUID:
        values: dict[str, Any] = {
            "id": uuid.uuid4(),
            "kind": kind,
            "dedup_key": dedup_key,
            "payload": dict(payload or {}),
            "status": PENDING,
            "correlation_id": correlation_id,
        }
        if max_attempts is not None:
            values["max_attempts"] = max_attempts
        inserted: UUID | None = await self._session.scalar(
            pg_insert(work_items)
            .values(values)
            .on_conflict_do_nothing(index_elements=[_c.dedup_key])
            .returning(_c.id)
        )
        if inserted is not None:
            return inserted
        # Conflit : l'item existe déjà (éventuellement terminé), on le renvoie.
        existing = await self._session.execute(
            select(_c.id).where(_c.dedup_key == dedup_key)
        )
        found: UUID = existing.scalar_one()
        return found

    async def cancel(self, dedup_key: str) -> bool:
        cancelled = await self._session.scalar(
            update(work_items)
            .where(_c.dedup_key == dedup_key, _c.status.in_((PENDING, RUNNING)))
            .values(
                status=CANCELLED,
                fencing_token=_c.fencing_token + 1,
                completed_at=func.now(),
                updated_at=func.now(),
                **_RELEASE_LEASE,
            )
            .returning(_c.id)
        )
        return cancelled is not None


class SqlAlchemyWorkLifecycle:
    """Opérations d'adapter utilisées par les tâches Celery.

    Chaque opération ouvre sa propre transaction courte, sauf ``complete`` et
    ``fail`` qui s'exécutent dans la transaction de finalisation fournie.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def select_dispatchable(
        self,
        queues: Collection[QueueCapacity],
        batch: int,
        redispatch_after: timedelta,
    ) -> list[tuple[UUID, str]]:
        """Marque ``dispatched_at`` et **commit** avant de rendre les items à
        notifier : la publication n'a lieu qu'après (S0 §8.2)."""
        notified_recently = and_(
            _c.status == PENDING, _c.dispatched_at >= func.now() - redispatch_after
        )
        selected: list[tuple[UUID, str]] = []
        async with self._session_factory() as session, session.begin():
            for capacity in queues:
                remaining = batch - len(selected)
                if remaining <= 0:
                    break
                in_flight = await session.scalar(
                    select(func.count())
                    .select_from(work_items)
                    .where(
                        _c.kind.in_(capacity.kinds),
                        or_(_c.status == RUNNING, notified_recently),
                    )
                )
                slots = min(remaining, capacity.max_in_flight - int(in_flight or 0))
                if slots <= 0:
                    continue
                rows = await session.execute(
                    select(_c.id, _c.kind)
                    .where(
                        _c.status == PENDING,
                        _c.kind.in_(capacity.kinds),
                        _c.available_at <= func.now(),
                        or_(
                            _c.dispatched_at.is_(None),
                            _c.dispatched_at < func.now() - redispatch_after,
                        ),
                    )
                    .order_by(_c.available_at)
                    .limit(slots)
                    .with_for_update(skip_locked=True)
                )
                selected.extend((row.id, row.kind) for row in rows)
            if selected:
                await session.execute(
                    update(work_items)
                    .where(_c.id.in_([item_id for item_id, _ in selected]))
                    .values(dispatched_at=func.now(), updated_at=func.now())
                )
        return selected

    async def claim(
        self,
        item_id: UUID,
        owner: str,
        lease_ttl: timedelta,
        *,
        max_attempts_by_kind: Mapping[str, int] | None = None,
    ) -> ClaimedWorkItem | None:
        """``max_attempts_by_kind`` (registre des handlers) fixe
        ``max_attempts`` dans le même UPDATE que le claim : la valeur déclarée
        par le handler s'applique dès la première tentative ; un ``kind`` absent
        garde la valeur de la colonne (défaut d'``enqueue``)."""
        max_attempts: Any = _c.max_attempts
        if max_attempts_by_kind:
            max_attempts = case(
                dict(max_attempts_by_kind), value=_c.kind, else_=_c.max_attempts
            )
        async with self._session_factory() as session, session.begin():
            row = (
                await session.execute(
                    update(work_items)
                    .where(
                        _c.id == item_id,
                        _c.status == PENDING,
                        _c.available_at <= func.now(),
                    )
                    .values(
                        status=RUNNING,
                        attempts=_c.attempts + 1,
                        max_attempts=max_attempts,
                        fencing_token=_c.fencing_token + 1,
                        lease_owner=owner,
                        lease_expires_at=func.now() + lease_ttl,
                        updated_at=func.now(),
                    )
                    .returning(
                        _c.id,
                        _c.kind,
                        _c.payload,
                        _c.attempts,
                        _c.max_attempts,
                        _c.fencing_token,
                        _c.correlation_id,
                    )
                )
            ).one_or_none()
        if row is None:
            return None
        return ClaimedWorkItem(
            id=row.id,
            kind=row.kind,
            payload=row.payload,
            attempts=row.attempts,
            max_attempts=row.max_attempts,
            fencing_token=row.fencing_token,
            correlation_id=row.correlation_id,
        )

    async def heartbeat(self, item_id: UUID, token: int, lease_ttl: timedelta) -> bool:
        async with self._session_factory() as session, session.begin():
            return await _touched(
                session,
                _fenced(item_id, token).values(
                    lease_expires_at=func.now() + lease_ttl, updated_at=func.now()
                ),
            )

    async def complete(self, session: AsyncSession, item_id: UUID, token: int) -> bool:
        return await _touched(
            session,
            _fenced(item_id, token).values(
                status=SUCCEEDED,
                completed_at=func.now(),
                updated_at=func.now(),
                **_RELEASE_LEASE,
            ),
        )

    async def retry_later(
        self, item_id: UUID, token: int, error: FailureInfo, delay: timedelta
    ) -> bool:
        async with self._session_factory() as session, session.begin():
            return await _touched(
                session,
                _fenced(item_id, token)
                .where(_c.attempts < _c.max_attempts)
                .values(
                    status=PENDING,
                    available_at=func.now() + delay,
                    dispatched_at=None,
                    updated_at=func.now(),
                    **_RELEASE_LEASE,
                    **_error_columns(error),
                ),
            )

    async def fail(
        self, session: AsyncSession, item_id: UUID, token: int, error: FailureInfo
    ) -> bool:
        return await _touched(
            session,
            _fenced(item_id, token).values(
                status=FAILED,
                completed_at=func.now(),
                updated_at=func.now(),
                **_RELEASE_LEASE,
                **_error_columns(error),
            ),
        )

    async def recover_expired(
        self, batch: int, *, backoff: Callable[[int], timedelta]
    ) -> int:
        """Reprend au plus ``batch`` leases expirés selon ``decide_recovery`` :
        ``PENDING`` avec ``available_at = now() + backoff(attempts)`` s'il reste
        des tentatives, ``FAILED`` sinon. Le token n'est pas incrémenté : c'est
        le claim suivant qui invalide la tentative perdue."""
        async with self._session_factory() as session, session.begin():
            expired: Sequence[Any] = (
                await session.execute(
                    select(_c.id, _c.attempts, _c.max_attempts)
                    .where(_c.status == RUNNING, _c.lease_expires_at < func.now())
                    .order_by(_c.lease_expires_at)
                    .limit(batch)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for row in expired:
                decision = decide_recovery(
                    attempts=row.attempts, max_attempts=row.max_attempts
                )
                assert decision.failure is not None  # jamais « ignore » ici
                values: dict[str, Any] = _error_columns(decision.failure)
                if decision.action == "retry":
                    values |= {
                        "status": PENDING,
                        "available_at": func.now() + backoff(row.attempts),
                        "dispatched_at": None,
                    }
                else:
                    values |= {"status": FAILED, "completed_at": func.now()}
                await session.execute(
                    update(work_items)
                    .where(_c.id == row.id)
                    .values(updated_at=func.now(), **_RELEASE_LEASE, **values)
                )
        return len(expired)


def _fenced(item_id: UUID, token: int) -> Update:
    """UPDATE conditionné à la tentative courante (S0 §5)."""
    return update(work_items).where(
        _c.id == item_id, _c.status == RUNNING, _c.fencing_token == token
    )


async def _touched(session: AsyncSession, statement: Update) -> bool:
    return await session.scalar(statement.returning(_c.id)) is not None
