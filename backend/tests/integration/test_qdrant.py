from __future__ import annotations

import pytest

from runtime.settings import Settings
from runtime.vector_store import check_connection, create_qdrant_client

pytestmark = pytest.mark.integration


async def test_async_client_reaches_qdrant(integration_settings: Settings) -> None:
    client = create_qdrant_client(integration_settings)
    try:
        assert await check_connection(client, timeout_seconds=5.0) is True
        collections = await client.get_collections()
        # Aucune collection métier n'existe en Phase 1.
        assert collections.collections == []
    finally:
        await client.close()
