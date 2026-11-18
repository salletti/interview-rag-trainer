"""La réconciliation comme travail durable (S8 ; ADR 0004 points 2, 7, 9, 10 ;
S0 §8.5), sur PostgreSQL, Qdrant et work queue réels.

Contrat vérifié :

- ``enqueue_reconciliation(session_factory, settings, *, now=None) -> UUID`` :
  un item ``reconcile_knowledge_base`` par CRÉNEAU de ``reconcile_interval`` ;
  deux planifications dans le même créneau (beat redémarré, deux instances)
  désignent le même item ; un autre créneau en crée un nouveau. Payload vide.
- Exécuté par la work queue, l'item applique un cycle complet puis passe
  ``SUCCEEDED``.
- Un cycle qui échoue (Qdrant injoignable) n'est JAMAIS réussi : l'item est
  rejoué avec backoff, puis ``FAILED`` à la dernière tentative, listé par
  ``GET /failures`` et rejouable (O10 : l'état ``FAILED`` est la dead-letter).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from knowledge_base import KnowledgeBaseWorkerResources
from knowledge_base.adapters.controller.tasks.execution import execute_work_item
from knowledge_base.adapters.controller.tasks.reconcile_handler import (
    build_reconcile_work_handler,
    enqueue_reconciliation,
)
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.indexing_support import QA_SHEET
from tests.integration.knowledge_base.s8_support import (
    PREFIX,
    S8Rig,
    open_s8_rig,
    settings_with,
)

pytestmark = pytest.mark.integration
KIND = "reconcile_knowledge_base"
SLOT = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)  # multiple de l'intervalle
INTERVAL = 600


@pytest.fixture
async def rig(
    integration_settings: Settings,
    session_factory: SessionFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[S8Rig]:
    async with open_s8_rig(
        integration_settings, session_factory, tmp_path, monkeypatch
    ) as opened:
        yield opened


async def enqueue(rig: S8Rig, now: datetime) -> UUID:
    return await enqueue_reconciliation(
        rig.session_factory,
        settings_with(reconcile_interval_seconds=INTERVAL),
        now=now,
    )


# --- planification par créneau -------------------------------------------------------


async def test_one_item_per_slot_whoever_asks(rig: S8Rig) -> None:
    first = await enqueue(rig, SLOT)
    same_slot = await enqueue(rig, SLOT + timedelta(seconds=INTERVAL - 1))

    assert same_slot == first
    (item,) = await rig.items(KIND)
    assert item["id"] == first
    assert (item["status"], item["payload"]) == ("PENDING", {})


async def test_the_next_slot_gets_its_own_item(rig: S8Rig) -> None:
    first = await enqueue(rig, SLOT)
    second = await enqueue(rig, SLOT + timedelta(seconds=INTERVAL))

    assert second != first
    assert len(await rig.items(KIND)) == 2


async def test_the_default_slot_is_the_current_time(rig: S8Rig) -> None:
    settings = settings_with(reconcile_interval_seconds=INTERVAL)

    one = await enqueue_reconciliation(rig.session_factory, settings)
    two = await enqueue_reconciliation(rig.session_factory, settings)

    assert one == two


# --- exécution par la work queue -----------------------------------------------------


async def test_the_scheduled_item_runs_a_cycle_and_succeeds(rig: S8Rig) -> None:
    imported = await rig.s7.publish(QA_SHEET)
    await rig.hide(imported.document_id, imported.version_id)
    assert await rig.found() == set()
    item_id = await enqueue(rig, SLOT)

    outcomes = await rig.drain()
    await rig.settle()

    assert (KIND, "succeeded") in outcomes
    reconcile = [i for i in await rig.items(KIND) if i["id"] == item_id]
    assert reconcile[0]["status"] == "SUCCEEDED"
    assert await rig.found() == {imported.document_id}


class UnreachableQdrant:
    """Qdrant dont le parcours des points échoue (réseau coupé) ; le reste
    délègue à l'adapter réel."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def scan_points(self, *args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("Qdrant injoignable")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def test_a_failing_cycle_is_retried_then_failed_listed_and_replayable(
    rig: S8Rig,
) -> None:
    await rig.s7.publish(QA_SHEET)
    broken = build_reconcile_work_handler(
        rig.session_factory, rig.settings, vector_index=UnreachableQdrant(rig.vector)
    )
    resources = KnowledgeBaseWorkerResources(
        session_factory=rig.session_factory,
        loop=asyncio.get_running_loop(),
        handlers=(broken,),
    )
    item_id = await enqueue(rig, SLOT)

    outcomes: list[str] = []
    for _ in range(broken.max_attempts):
        outcomes.append(
            await execute_work_item(item_id, resources, rig.settings, owner="t")
        )
        if len(outcomes) == 1:
            (item,) = await rig.items(KIND)
            assert (item["status"], item["attempts"]) == ("PENDING", 1)
            assert item["last_error_code"] in {"UNEXPECTED_ERROR", "TRANSIENT"}
        await asyncio.sleep(0.05)  # backoff de test : 10 ms
    outcome = outcomes[-1]
    assert outcomes[0] == "retry_scheduled"
    assert outcome == "failed"
    (failed,) = await rig.items(KIND)
    assert failed["status"] == "FAILED"
    assert failed["attempts"] == broken.max_attempts
    listed = (await rig.http.get(f"{PREFIX}/failures")).json()["items"]
    assert [(i["kind"], i["replayable"]) for i in listed] == [(KIND, True)]
    retried = await rig.http.post(f"{PREFIX}/failures/{item_id}/retry")
    assert retried.status_code == 202, retried.text
