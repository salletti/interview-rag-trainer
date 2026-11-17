"""Sonde de disponibilité du broker RabbitMQ.

Kombu (dépendance de Celery) est synchrone : la connexion est ouverte dans un
thread et bornée par un délai côté asyncio pour ne jamais bloquer la boucle
d'événements de l'API.
"""

from __future__ import annotations

import asyncio

from kombu.connection import Connection

from runtime.settings import Settings


def _probe(broker_url: str, timeout: float) -> None:
    with Connection(broker_url, connect_timeout=timeout) as connection:
        connection.ensure_connection(max_retries=0, timeout=timeout)


async def check_connection(settings: Settings, timeout_seconds: float) -> bool:
    """Vrai si une connexion AMQP s'établit dans le délai imparti."""
    try:
        async with asyncio.timeout(timeout_seconds):
            await asyncio.to_thread(_probe, settings.broker_url, timeout_seconds)
    except Exception:  # toute erreur réseau vaut « indisponible »
        return False
    return True
