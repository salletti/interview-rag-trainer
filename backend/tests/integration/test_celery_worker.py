"""Aller-retour complet broker → worker → result backend.

Le worker est le service ``worker_test`` de ``compose.test.yaml`` (même image
que la stack de développement) : le test valide le broker RabbitMQ, la
sérialisation JSON, l'exécution de la tâche par un vrai worker et le result
backend ``rpc://``.
"""

from __future__ import annotations

import pytest

from runtime import messaging
from runtime.settings import Settings
from runtime.worker import ping

pytestmark = pytest.mark.integration


async def test_broker_is_reachable(integration_settings: Settings) -> None:
    assert await messaging.check_connection(integration_settings, 5.0) is True


def test_ping_round_trip_through_rabbitmq() -> None:
    result = ping.apply_async()
    assert result.get(timeout=20) == "pong"
