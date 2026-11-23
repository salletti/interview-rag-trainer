"""Refus de ``StartIndexMigration``, sans aucun effet (ni collection, ni
écriture)."""

from __future__ import annotations

from uuid import UUID

from knowledge_base.domain.exceptions.corpus_errors import IndexOperationRefusedError
from knowledge_base.domain.value_objects.chunking_policy import ChunkingPolicy
from knowledge_base.domain.value_objects.embedding_profile import EmbeddingProfile


class StartIndexMigrationError(IndexOperationRefusedError):
    """Base des refus du démarrage d'une migration."""


class NoActiveIndexToMigrateError(StartIndexMigrationError):
    """Le corpus n'a pas d'index actif : le premier index est provisionné au
    démarrage de la stack (``python -m runtime.provision``), pas migré."""

    def __init__(self) -> None:
        super().__init__(
            "aucun index actif à migrer : provisionner d'abord l'index initial "
            "(python -m runtime.provision)"
        )


class MigrationInProgressError(StartIndexMigrationError):
    """Un index ``BUILDING`` d'un autre couple (profil, politique) existe :
    un seul backfill à la fois (une seule migration en cours). Levée d'emblée
    si la première lecture le voit, ou en traduction de
    ``IndexAlreadyBuildingError`` (agrégat) si une migration rivale a validé
    entre les deux transactions."""

    def __init__(
        self,
        building_id: UUID,
        profile: EmbeddingProfile,
        chunking_policy: ChunkingPolicy,
    ) -> None:
        super().__init__(
            f"migration déjà en cours vers l'index {building_id} "
            f"({profile.model}@{profile.revision[:8]}, {chunking_policy.identity}) "
            ": la terminer (python -m runtime.index_migration activate "
            f"{building_id}) ou l'abandonner (python -m runtime.index_migration "
            f"abandon {building_id}) avant d'en démarrer une autre"
        )
        self.building_id = building_id


class NothingToMigrateError(StartIndexMigrationError):
    """Le profil et la politique demandés sont ceux de l'index actif."""

    def __init__(self, active_id: UUID) -> None:
        super().__init__(
            f"l'index actif {active_id} a déjà ce profil et cette politique : "
            "rien à migrer"
        )
        self.active_id = active_id
