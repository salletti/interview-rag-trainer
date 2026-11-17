"""Point d'initialisation du client Qdrant (moteur vectoriel principal).

Aucune collection métier n'est créée ici : le pipeline RAG et ses
collections appartiendront au contexte qui en aura besoin (Knowledge Base,
envisagé). Ce module fournit uniquement le client asynchrone du processus et
une sonde de disponibilité.
"""

from __future__ import annotations

import asyncio
import math

from qdrant_client import AsyncQdrantClient

from runtime.settings import Settings


def create_qdrant_client(settings: Settings) -> AsyncQdrantClient:
    api_key = settings.qdrant_api_key
    return AsyncQdrantClient(
        url=settings.qdrant_url,
        api_key=api_key.get_secret_value() if api_key is not None else None,
        # Le client n'accepte qu'un entier : arrondi au-dessus, jamais à zéro.
        timeout=max(1, math.ceil(settings.external_timeout_seconds)),
        # Le constructeur ferait sinon un appel HTTP synchrone de contrôle de
        # version : la disponibilité est vérifiée par ``check_connection``.
        check_compatibility=False,
    )


async def check_connection(client: AsyncQdrantClient, timeout_seconds: float) -> bool:
    """Vrai si Qdrant répond à une requête triviale dans le délai imparti."""
    try:
        async with asyncio.timeout(timeout_seconds):
            await client.get_collections()
    except Exception:  # toute erreur réseau vaut « indisponible »
        return False
    return True
