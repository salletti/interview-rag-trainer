"""Aides partagées par les tests (importées comme ``tests.support``)."""

from __future__ import annotations

from pydantic import SecretStr

from runtime.settings import Settings


def unreachable_settings(**overrides: object) -> Settings:
    """Configuration dont toutes les dépendances externes sont injoignables :
    le port 1 n'est jamais écouté, la connexion est refusée immédiatement."""
    base: dict[str, object] = {
        "environment": "test",
        "external_timeout_seconds": 0.5,
        "postgres_host": "127.0.0.1",
        "postgres_port": 1,
        "postgres_password": SecretStr("unused"),
        "qdrant_host": "127.0.0.1",
        "qdrant_port": 1,
        "rabbitmq_host": "127.0.0.1",
        "rabbitmq_port": 1,
    }
    base.update(overrides)
    return Settings.model_validate(base)
