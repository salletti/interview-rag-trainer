"""Scénario 12 : migration ``upgrade head`` / ``downgrade -1`` / ``upgrade
head`` et conformité du schéma ``knowledge_base.work_items`` au §2.

Alembic est lancé comme sous-processus (environnement POSTGRES_* hérité), comme
``make test-integration``. L'aller-retour est ancré sur la révision de S0
(``S0_REVISION``) et non sur ``head`` : redescendre sous S0 supprime
``knowledge_base.work_items``, quel que soit le nombre de migrations
postérieures.
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from runtime.database import SessionFactory

pytestmark = pytest.mark.integration

BACKEND = Path(__file__).resolve().parents[3]
S0_REVISION = "f35542284e25"
BEFORE_S0_REVISION = "7c1a0e2b9d41"

# colonne -> (type PostgreSQL, nullable)
EXPECTED_COLUMNS = {
    "id": ("uuid", False),
    "kind": ("text", False),
    "payload": ("jsonb", False),
    "dedup_key": ("text", False),
    "status": ("text", False),
    "attempts": ("integer", False),
    "max_attempts": ("integer", False),
    "fencing_token": ("bigint", False),
    "lease_owner": ("text", True),
    "lease_expires_at": ("timestamp with time zone", True),
    "available_at": ("timestamp with time zone", False),
    "dispatched_at": ("timestamp with time zone", True),
    "last_error_code": ("text", True),
    "last_error_message": ("text", True),
    "correlation_id": ("uuid", True),
    "created_at": ("timestamp with time zone", False),
    "updated_at": ("timestamp with time zone", False),
    "completed_at": ("timestamp with time zone", True),
    # S8 (migration 5d2f8c1a7b30) : réouvertures d'un item FAILED.
    "reopen_count": ("integer", False),
}


def alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(BACKEND / "alembic.ini"), *args],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def alembic_ok(*args: str) -> str:
    result = alembic(*args)
    assert result.returncode == 0, f"alembic {' '.join(args)}\n{result.stderr}"
    return result.stdout + result.stderr


@pytest.fixture
def restore_head() -> Iterator[None]:
    """Quoi qu'il arrive, la base est laissée à ``head`` pour les autres tests."""
    yield
    alembic("upgrade", "head")


async def table_exists(factory: SessionFactory) -> bool:
    async with factory() as session:
        found = await session.scalar(
            text("SELECT to_regclass('knowledge_base.work_items') IS NOT NULL")
        )
    return bool(found)


async def test_upgrade_downgrade_upgrade_round_trip(
    session_factory: SessionFactory, restore_head: None
) -> None:
    alembic_ok("upgrade", "head")
    assert await table_exists(session_factory)

    alembic_ok("downgrade", BEFORE_S0_REVISION)
    assert not await table_exists(session_factory)
    assert BEFORE_S0_REVISION in alembic_ok("current")

    alembic_ok("upgrade", S0_REVISION)
    assert await table_exists(session_factory)
    assert S0_REVISION in alembic_ok("current")

    alembic_ok("upgrade", "head")
    assert await table_exists(session_factory)


async def test_the_alembic_version_table_stays_in_the_public_schema(
    session_factory: SessionFactory,
) -> None:
    async with session_factory() as session:
        schema = await session.scalar(
            text(
                "SELECT table_schema FROM information_schema.tables "
                "WHERE table_name = 'alembic_version'"
            )
        )
    assert schema == "public"


async def test_the_models_and_the_migration_do_not_drift() -> None:
    # ``alembic check`` échoue si la migration et les MetaData de alembic/env.py
    # (include_schemas=True) divergent.
    alembic_ok("check")


async def test_work_items_columns_match_the_design(
    session_factory: SessionFactory,
) -> None:
    async with session_factory() as session:
        rows = await session.execute(
            text(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema = 'knowledge_base' AND table_name = 'work_items'"
            )
        )
        actual = {name: (kind, nullable == "YES") for name, kind, nullable in rows}

    assert actual == EXPECTED_COLUMNS


async def test_work_items_indexes_match_the_design(
    session_factory: SessionFactory,
) -> None:
    async with session_factory() as session:
        rows = await session.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = 'knowledge_base' AND tablename = 'work_items'"
            )
        )
        indexes: dict[str, str] = {str(r[0]): str(r[1]) for r in rows}

    dispatchable = indexes["ix_work_items_dispatchable"]
    assert "(available_at)" in dispatchable
    assert "PENDING" in dispatchable
    running_lease = indexes["ix_work_items_running_lease"]
    assert "(lease_expires_at)" in running_lease
    assert "RUNNING" in running_lease
    assert "(kind, status)" in indexes["ix_work_items_kind_status"]
    unique = [d for d in indexes.values() if "UNIQUE" in d and "(dedup_key)" in d]
    assert len(unique) == 1


def row(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": uuid.uuid4(),
        "kind": "test.kind",
        "dedup_key": f"k:{uuid.uuid4()}",
        "status": "PENDING",
        "max_attempts": 5,
    }
    base.update(overrides)
    return base


async def insert_row(factory: SessionFactory, **overrides: object) -> dict[str, object]:
    values = row(**overrides)
    columns = ", ".join(values)
    params = ", ".join(f":{name}" for name in values)
    async with factory() as session, session.begin():
        await session.execute(
            text(
                f"INSERT INTO knowledge_base.work_items ({columns}, created_at, "
                f"updated_at) VALUES ({params}, now(), now())"
            ),
            values,
        )
    return values


async def test_defaults_apply_to_a_minimal_insert(
    session_factory: SessionFactory,
) -> None:
    values = await insert_row(session_factory)

    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT payload, attempts, fencing_token, available_at <= now() "
                "FROM knowledge_base.work_items WHERE id = :id"
            ),
            {"id": values["id"]},
        )
        payload, attempts, token, available = result.one()
    assert (payload, attempts, token, available) == ({}, 0, 0, True)


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "DONE"},
        {"attempts": -1},
        {"max_attempts": 0},
        {"status": "RUNNING", "lease_expires_at": None},
    ],
    ids=[
        "unknown-status",
        "negative-attempts",
        "zero-max-attempts",
        "running-no-lease",
    ],
)
async def test_check_constraints_reject_invalid_rows(
    session_factory: SessionFactory, overrides: dict[str, object]
) -> None:
    with pytest.raises(IntegrityError):
        await insert_row(session_factory, **overrides)


async def test_a_running_row_with_a_lease_is_accepted(
    session_factory: SessionFactory,
) -> None:
    async with session_factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO knowledge_base.work_items (id, kind, dedup_key, "
                "status, max_attempts, lease_expires_at, created_at, updated_at) "
                "VALUES (:id, 'k', :key, 'RUNNING', 5, now() + interval '1 minute', "
                "now(), now())"
            ),
            {"id": uuid.uuid4(), "key": f"k:{uuid.uuid4()}"},
        )


async def test_dedup_key_is_unique(session_factory: SessionFactory) -> None:
    await insert_row(session_factory, dedup_key="same")

    with pytest.raises(IntegrityError):
        await insert_row(session_factory, dedup_key="same")


async def test_every_documented_status_is_accepted(
    session_factory: SessionFactory,
) -> None:
    for status in ("PENDING", "SUCCEEDED", "FAILED", "CANCELLED"):
        await insert_row(session_factory, status=status)
