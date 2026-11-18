"""Réglages techniques de Knowledge Base : work queue (S0 §7, §8.4), import et
stockage des originaux (S2, ADR 0012), embeddings (S4, ADR 0008 §6).

Les valeurs sont des défauts **Proposed** : configurables par variables
``KB_*`` et à ajuster sur mesure.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class KnowledgeBaseSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KB_",
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    work_backoff_base_seconds: float = Field(default=10, gt=0)
    work_backoff_max_seconds: float = Field(default=900, gt=0)
    work_lease_ttl_seconds: float = Field(default=300, gt=0)
    # Absent : un tiers du lease (S0 §7).
    work_heartbeat_interval_seconds: float | None = Field(default=None, gt=0)
    work_time_limit_seconds: float = Field(default=3600, gt=0)
    work_dispatch_interval_seconds: float = Field(default=2, gt=0)
    work_dispatch_batch: int = Field(default=50, gt=0)
    work_redispatch_after_seconds: float = Field(default=600, gt=0)
    # Plafond d'items en vol, appliqué à chaque file de travail (S0 §8.2) ;
    # en S0, la seule file est celle de l'ingestion.
    work_max_in_flight: int = Field(default=2, gt=0)
    work_recover_interval_seconds: float = Field(default=30, gt=0)

    # Import (ADR 0012) : taille maximale d'un original et racine du volume
    # des blobs, montée dans l'API et les workers (compose.yaml, kb_blobs).
    max_upload_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    blob_root: Path = Path("/var/lib/interview-rag-trainer/blobs")

    # Embeddings (S4) : poids des modèles dans le volume models_cache, monté
    # dans l'API et le worker d'ingestion ; téléchargés au premier chargement.
    models_cache_dir: Path = Path("/var/cache/interview-rag-trainer/models")
    # Chunks par appel au modèle, donc par upsert Qdrant.
    embedding_batch_size: int = Field(default=32, gt=0)

    @model_validator(mode="after")
    def _heartbeat_shorter_than_lease(self) -> Self:
        heartbeat = self.work_heartbeat_interval_seconds
        if heartbeat is not None and heartbeat >= self.work_lease_ttl_seconds:
            raise ValueError(
                "work_heartbeat_interval_seconds doit être strictement inférieur "
                f"à work_lease_ttl_seconds ({heartbeat} >= "
                f"{self.work_lease_ttl_seconds}) : le lease expirerait entre "
                "deux heartbeats"
            )
        return self

    @property
    def heartbeat_interval_seconds(self) -> float:
        if self.work_heartbeat_interval_seconds is not None:
            return self.work_heartbeat_interval_seconds
        return self.work_lease_ttl_seconds / 3

    @property
    def lease_ttl(self) -> timedelta:
        return timedelta(seconds=self.work_lease_ttl_seconds)

    @property
    def heartbeat_interval(self) -> timedelta:
        return timedelta(seconds=self.heartbeat_interval_seconds)

    @property
    def redispatch_after(self) -> timedelta:
        return timedelta(seconds=self.work_redispatch_after_seconds)

    @property
    def backoff_base(self) -> timedelta:
        return timedelta(seconds=self.work_backoff_base_seconds)

    @property
    def backoff_cap(self) -> timedelta:
        return timedelta(seconds=self.work_backoff_max_seconds)
