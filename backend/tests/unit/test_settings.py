from __future__ import annotations

from pydantic import SecretStr

from runtime.settings import Settings


def test_secrets_are_not_printed_by_repr() -> None:
    settings = Settings(
        postgres_password=SecretStr("pg-secret"),
        rabbitmq_password=SecretStr("mq-secret"),
        qdrant_api_key=SecretStr("qd-secret"),
    )

    dumped = repr(settings) + str(settings.model_dump())
    assert "pg-secret" not in dumped
    assert "mq-secret" not in dumped
    assert "qd-secret" not in dumped


def test_urls_are_built_from_components() -> None:
    settings = Settings(
        postgres_host="db",
        postgres_port=5433,
        postgres_user="u",
        postgres_password=SecretStr("p"),
        postgres_db="d",
        rabbitmq_host="mq",
        rabbitmq_user="ru",
        rabbitmq_password=SecretStr("rp"),
        rabbitmq_vhost="/",
        qdrant_host="qd",
        qdrant_port=6333,
    )

    assert settings.database_url == "postgresql+asyncpg://u:p@db:5433/d"
    assert settings.broker_url == "amqp://ru:rp@mq:5672/"
    assert settings.qdrant_url == "http://qd:6333"


def test_docs_only_outside_production() -> None:
    assert Settings(environment="development").docs_enabled is True
    assert Settings(environment="test").docs_enabled is True
    assert Settings(environment="production").docs_enabled is False
