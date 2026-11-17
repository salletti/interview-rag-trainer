"""Configuration technique du processus, lue une fois depuis l'environnement.

Ne pas confondre avec une future configuration métier (barèmes, politiques de
session) : ici il n'y a que des hôtes, ports, identifiants et délais.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "production"]


class Settings(BaseSettings):
    """Paramètres techniques. Les secrets sont des ``SecretStr`` : jamais
    sérialisés en clair par accident (``repr``, logs, réponses HTTP)."""

    # ``.env`` à la racine du dépôt (lancement depuis ``backend/``) ou dans le
    # répertoire courant ; les variables d'environnement l'emportent toujours.
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Interview RAG Trainer"
    app_version: str = "0.1.0"
    environment: Environment = "development"
    log_level: str = "INFO"

    # Délai maximal accordé à toute vérification d'une dépendance externe
    # (readiness). Chaque sonde est bornée individuellement.
    external_timeout_seconds: float = Field(default=2.0, gt=0)

    # Défauts alignés sur les ports hôte de compose.yaml : un lancement hors
    # Docker, sans .env, vise la stack de développement.
    postgres_host: str = "localhost"
    postgres_port: int = 5440
    postgres_user: str = "interview_rag_trainer"
    postgres_password: SecretStr = SecretStr("interview_rag_trainer")
    postgres_db: str = "interview_rag_trainer"

    qdrant_host: str = "localhost"
    qdrant_port: int = 6333
    qdrant_grpc_port: int = 6334
    qdrant_api_key: SecretStr | None = None

    rabbitmq_host: str = "localhost"
    rabbitmq_port: int = 5672
    rabbitmq_user: str = "interview_rag_trainer"
    rabbitmq_password: SecretStr = SecretStr("interview_rag_trainer")
    rabbitmq_vhost: str = "/"

    # Result backend Celery. ``rpc://`` réutilise RabbitMQ (réponses via une
    # file AMQP dédiée au client) : aucun Redis n'est nécessaire pour le
    # socle technique. Voir docs/decisions/0006-technical-bootstrap.md.
    celery_result_backend: str = "rpc://"

    @property
    def database_url(self) -> str:
        """DSN SQLAlchemy asynchrone (driver asyncpg)."""
        return (
            f"postgresql+asyncpg://{self.postgres_user}:"
            f"{self.postgres_password.get_secret_value()}@"
            f"{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def qdrant_url(self) -> str:
        return f"http://{self.qdrant_host}:{self.qdrant_port}"

    @property
    def broker_url(self) -> str:
        """URL AMQP du broker Celery."""
        vhost = self.rabbitmq_vhost.lstrip("/")
        return (
            f"amqp://{self.rabbitmq_user}:"
            f"{self.rabbitmq_password.get_secret_value()}@"
            f"{self.rabbitmq_host}:{self.rabbitmq_port}/{vhost}"
        )

    @property
    def docs_enabled(self) -> bool:
        """La documentation OpenAPI n'est servie qu'en dehors de la production."""
        return self.environment != "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Singleton processus : les variables d'environnement ne changent pas
    pendant la vie du processus."""
    return Settings()
