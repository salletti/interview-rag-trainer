"""Réglages techniques de Knowledge Base : work queue (S0 §7, §8.4), import et
stockage des originaux (S2, ADR 0012), embeddings (S4, ADR 0008 §6),
recherche (S5, knowledge-base.md §12, O12), nettoyage des blobs orphelins
(S7, ADR 0012 point 6), réconciliation, expiration des clés d'idempotence et
borne de volume de texte (S8, O15, O16), délai de suppression de la collection
d'un index retiré (S9).

Les valeurs sont des défauts **Proposed** : configurables par variables
``KB_*`` et à ajuster sur mesure.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from knowledge_base.frameworks.configuration.chunking import DEFAULT_CHUNKING_POLICY


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
    # Âge minimal d'un blob non référencé avant sa suppression (ADR 0012,
    # Proposed : 24 h) : doit dépasser largement la durée d'un import, dont
    # le dépôt précède la transaction.
    blob_orphan_grace_seconds: float = Field(default=24 * 3600, gt=0)

    # Embeddings (S4) : poids des modèles dans le volume models_cache, monté
    # dans l'API et le worker d'ingestion ; téléchargés au premier chargement.
    models_cache_dir: Path = Path("/var/cache/interview-rag-trainer/models")
    # Chunks par appel au modèle, donc par upsert Qdrant.
    embedding_batch_size: int = Field(default=32, gt=0)

    # Recherche (S5, O12) : suréchantillonnage de la première passe
    # (``limit x f`` unités demandées à Qdrant), passes de complément après
    # le contrôle SQL, budget en tokens d'un passage reconstruit
    # (small-to-big). Le budget contient au moins un chunk entier de la
    # politique de production (``DEFAULT_CHUNKING_POLICY.max_tokens``, 508) :
    # en dessous, le meilleur chunk d'une unité longue dépasserait sa fenêtre
    # (il serait alors rendu seul, hors budget).
    search_oversampling_factor: int = Field(default=2, ge=1)
    search_complement_passes: int = Field(default=1, ge=0)
    max_passage_tokens: int = Field(default=1024, ge=DEFAULT_CHUNKING_POLICY.max_tokens)

    # Réconciliation (S8, ADR 0004 point 9) : période du beat qui planifie un
    # cycle (un work item par créneau, file d'ingestion) ; indexations
    # COMPLETED dont le manifest est revérifié par cycle (échantillon) ;
    # réouvertures automatiques d'une purge FAILED au-delà desquelles elle
    # attend un rejeu explicite (Qdrant durablement en panne : pas de boucle
    # au rythme du cycle).
    reconcile_interval_seconds: float = Field(default=300, gt=0)
    reconcile_manifest_sample_size: int = Field(default=20, ge=0)
    reconcile_max_purge_reopenings: int = Field(default=3, ge=0)
    # Âge au-delà duquel une clé d'idempotence est purgée par la
    # réconciliation (O15, 24 h) : un rejeu plus tardif de la même requête
    # n'est plus reconnu, la contrainte UNIQUE(content_hash) évite tout doublon.
    idempotency_key_ttl_seconds: float = Field(default=24 * 3600, gt=0)
    # Borne du volume de texte extrait, tous formats (O16) : somme des
    # token_count des unités. Origine du défaut (knowledge-base.md §16, O16) :
    # 3,74 caractères par token mesurés (tokenizer E5, fixtures FR/EN) ; une
    # page A4 très dense (~5 000 caractères) fait ~1 340 tokens, 300 pages
    # ~400 000 ; le PDF de 300 pages de S6 (410 unités, 682 chunks d'au plus
    # 508 tokens) reste sous 350 000. 1 000 000 garde plus de 2x de marge sur
    # un PDF légitime et borne l'indexation (~2 200 chunks à 0,46 s, ~17 min)
    # sous la limite de 3 600 s d'une tâche.
    max_document_tokens: int = Field(default=1_000_000, gt=0)

    # Migration blue/green (S9) : délai entre l'activation d'un nouvel index et
    # la suppression de la collection de l'ancien. Une recherche lit l'index
    # actif, PUIS interroge Qdrant : commencée avant la bascule, elle peut
    # encore lire l'ancienne collection. Le délai doit donc dépasser la durée
    # d'une recherche ; 300 s couvrent largement le chargement d'un modèle à la
    # première requête (4,3 s mesurées en S5) et le délai d'une requête Qdrant
    # (EXTERNAL_TIMEOUT_SECONDS, 2 s par défaut).
    index_drop_delay_seconds: float = Field(default=300, ge=0)

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
    def blob_orphan_grace(self) -> timedelta:
        return timedelta(seconds=self.blob_orphan_grace_seconds)

    @property
    def backoff_cap(self) -> timedelta:
        return timedelta(seconds=self.work_backoff_max_seconds)

    @property
    def reconcile_interval(self) -> timedelta:
        return timedelta(seconds=self.reconcile_interval_seconds)

    @property
    def index_drop_delay(self) -> timedelta:
        return timedelta(seconds=self.index_drop_delay_seconds)

    @property
    def idempotency_key_ttl(self) -> timedelta:
        return timedelta(seconds=self.idempotency_key_ttl_seconds)
