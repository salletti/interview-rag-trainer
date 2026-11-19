"""Décisions de la réconciliation : des faits en entrée, une décision en
sortie, sans aucune entrée-sortie (S8 ; knowledge-base.md §5.3, §7.3
condition 6, §8, §9, §10, §11, O14).

Le cas d'usage ``ReconcileKnowledgeBase`` lit Qdrant, PUIS PostgreSQL, et
confie ici chaque jugement ; il n'applique que ce que ces règles décident.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from knowledge_base.domain.entities.indexation import IndexationStatus
from knowledge_base.domain.entities.knowledge_corpus import IndexStatus
from knowledge_base.domain.entities.knowledge_document import (
    DocumentStatus,
    VersionStatus,
)
from knowledge_base.domain.value_objects.index_manifest import IndexManifest
from knowledge_base.domain.value_objects.indexed_point import PointSelector


class DiscrepancyKind(StrEnum):
    """Ce qu'un cycle trouve. Unité du décompte : des POINTS pour
    ``ORPHAN_POINTS``, ``HIDDEN_PUBLISHED_POINTS`` et
    ``WITHDRAWN_DOCUMENT_POINTS`` ; des entités (versions, indexations,
    items, fichiers, clés) pour les autres."""

    # Indexation COMPLETED dans l'index actif d'une version publiable non
    # publiée (publication perdue) : publiée.
    COMPLETED_NOT_PUBLISHED = "COMPLETED_NOT_PUBLISHED"
    # Version EXTRACTED d'un document actif sans indexation dans un index
    # vivant de sa politique : indexation PENDING et item index_version.
    EXTRACTED_WITHOUT_INDEXATION = "EXTRACTED_WITHOUT_INDEXATION"
    # Version d'un document actif que le backfill d'un index BUILDING ne
    # couvre pas (S9) : publiée, ou extraite sous une autre politique que
    # celle de l'index (la règle précédente ne voit que sa politique).
    # Indexation PENDING et item index_version ou rechunk_version.
    MISSING_FROM_BUILDING_INDEX = "MISSING_FROM_BUILDING_INDEX"
    # Points sans propriétaire légitime (document ou version inconnus,
    # version SUPERSEDED ou DISCARDED), ou hors du manifest d'une version
    # vérifiée : supprimés.
    ORPHAN_POINTS = "ORPHAN_POINTS"
    # Manifest d'une indexation COMPLETED qui ne se vérifie plus : rouverte.
    MANIFEST_DRIFT = "MANIFEST_DRIFT"
    # Points invisibles d'une version publiée d'un document actif, sans
    # synchronisation en attente : sync_visibility replanifiée.
    HIDDEN_PUBLISHED_POINTS = "HIDDEN_PUBLISHED_POINTS"
    # Points d'un document retiré ET purgé (tentative zombie après la
    # purge) : purge_document replanifiée.
    WITHDRAWN_DOCUMENT_POINTS = "WITHDRAWN_DOCUMENT_POINTS"
    # Item FAILED dont le hook d'échec n'a pas abouti (O14) : échec métier
    # écrit à sa place.
    FAILED_WORK_WITHOUT_STATE = "FAILED_WORK_WITHOUT_STATE"
    # Item purge_document FAILED d'un document retiré non purgé : rouvert,
    # ou laissé au rejeu explicite au-delà de la borne de réouvertures.
    FAILED_PURGE = "FAILED_PURGE"
    # Fichiers laissés sous quarantine/ par un crash pendant la suppression
    # conditionnelle d'un orphelin : remis en place ou supprimés.
    QUARANTINED_BLOB = "QUARANTINED_BLOB"
    ORPHAN_BLOB = "ORPHAN_BLOB"
    STALE_UPLOAD = "STALE_UPLOAD"
    EXPIRED_IDEMPOTENCY_KEY = "EXPIRED_IDEMPOTENCY_KEY"


# --- points de l'index ---------------------------------------------------------------


@dataclass(frozen=True)
class PointFacts:
    """Un point lu dans Qdrant (ou un groupe de points de même propriétaire et
    de même visibilité)."""

    document_id: UUID
    version_id: UUID
    visible: bool


@dataclass(frozen=True)
class DocumentFacts:
    """L'état SQL d'un document, lu APRÈS le parcours de Qdrant : un point
    écrit après une lecture SQL antérieure serait pris pour un orphelin et
    supprimé. Les versions ne disparaissent jamais de PostgreSQL (tombstones) :
    lire le SQL en dernier peut faire manquer un point de trop, jamais
    supprimer un point légitime."""

    status: DocumentStatus
    purged: bool
    versions: Mapping[UUID, VersionStatus]


class PointVerdict(StrEnum):
    OK = "OK"
    ORPHAN = "ORPHAN"
    RESYNC = "RESYNC"
    REPURGE = "REPURGE"


_OBSOLETE_VERSIONS = frozenset({VersionStatus.SUPERSEDED, VersionStatus.DISCARDED})


def judge_point(point: PointFacts, document: DocumentFacts | None) -> PointVerdict:
    if document is None:
        return PointVerdict.ORPHAN
    version = document.versions.get(point.version_id)
    if version is None:
        return PointVerdict.ORPHAN
    if document.status is DocumentStatus.WITHDRAWN:
        # Non purgé : la purge planifiée par le retrait s'en chargera.
        return PointVerdict.REPURGE if document.purged else PointVerdict.OK
    if version in _OBSOLETE_VERSIONS:
        return PointVerdict.ORPHAN
    if version is VersionStatus.PUBLISHED and not point.visible:
        return PointVerdict.RESYNC
    # Version en cours d'indexation ou à publier : invisible, c'est normal.
    # Un point visible d'une version non publiée ne sort jamais d'une
    # recherche (contrôle SQL, §11) et aucun travail ne le corrigerait.
    return PointVerdict.OK


@dataclass(frozen=True)
class PointCorrections:
    delete: frozenset[PointSelector]
    resync: frozenset[UUID]
    repurge: frozenset[UUID]


def plan_point_corrections(
    points: Iterable[PointFacts], documents: Mapping[UUID, DocumentFacts]
) -> PointCorrections:
    """Une sélection par version orpheline (par document s'il est inconnu),
    une synchronisation ou une purge par document."""
    delete: set[PointSelector] = set()
    resync: set[UUID] = set()
    repurge: set[UUID] = set()
    for point in points:
        document = documents.get(point.document_id)
        match judge_point(point, document):
            case PointVerdict.ORPHAN if document is None:
                delete.add(PointSelector(point.document_id))
            case PointVerdict.ORPHAN:
                delete.add(PointSelector(point.document_id, point.version_id))
            case PointVerdict.RESYNC:
                resync.add(point.document_id)
            case PointVerdict.REPURGE:
                repurge.add(point.document_id)
            case PointVerdict.OK:
                pass
    return PointCorrections(
        delete=frozenset(delete), resync=frozenset(resync), repurge=frozenset(repurge)
    )


# --- manifest -------------------------------------------------------------------------


@dataclass(frozen=True)
class ManifestDrift:
    missing: frozenset[UUID]
    different: frozenset[UUID]
    digest_changed: bool


def compare_manifest(
    expected: IndexManifest, recorded_digest: str, found: Mapping[UUID, str]
) -> ManifestDrift | None:
    """``found`` : ``chunk_hash`` des points présents dans l'index. Un écart
    est un point attendu absent, une empreinte différente, ou un digest
    enregistré qui n'est plus celui des chunks (I8 ne tient plus)."""
    entries = dict(expected.entries)
    missing = frozenset(entries.keys() - found.keys())
    different = frozenset(
        point
        for point, digest in entries.items()
        if point in found and found[point] != digest
    )
    digest_changed = expected.digest != recorded_digest
    if not (missing or different or digest_changed):
        return None
    return ManifestDrift(
        missing=missing, different=different, digest_changed=digest_changed
    )


def points_beyond_manifest(
    expected: IndexManifest, found: Collection[UUID]
) -> frozenset[UUID]:
    """Points d'une version vérifiée absents de son manifest : « points en
    trop » (§9), supprimés par identifiant."""
    return frozenset(found) - {point for point, _ in expected.entries}


# --- indexations ----------------------------------------------------------------------


def indexes_missing_indexation(
    *,
    document_status: DocumentStatus,
    version_status: VersionStatus,
    live_index_ids: Collection[UUID],
    indexed_index_ids: Collection[UUID],
) -> frozenset[UUID]:
    """Index vivants (de la politique du lot de la version) où une version
    ``EXTRACTED`` d'un document actif n'a aucune indexation. Une indexation
    existante, quel que soit son état, est un fait : ses échecs relèvent des
    items ``FAILED``, pas de cette règle."""
    if (
        document_status is not DocumentStatus.ACTIVE
        or version_status is not VersionStatus.EXTRACTED
    ):
        return frozenset()
    return frozenset(live_index_ids) - frozenset(indexed_index_ids)


def building_indexes_missing_version(
    *,
    document_status: DocumentStatus,
    version_status: VersionStatus,
    policy_versions: Collection[str],
    building_policies: Mapping[UUID, str],
    indexed_index_ids: Collection[UUID],
) -> frozenset[UUID]:
    """Index ``BUILDING`` (identifiant -> politique) sans indexation de la
    version, que ne couvre pas ``indexes_missing_indexation`` : version
    ``PUBLISHED`` d'un document actif, quelle que soit la politique ; version
    ``EXTRACTED`` seulement si l'index est d'une politique dont elle n'a pas de
    lot. Sans cela, une indexation perdue bloquerait l'activation pour
    toujours (I11)."""
    if document_status is not DocumentStatus.ACTIVE:
        return frozenset()
    if version_status is VersionStatus.PUBLISHED:
        candidates = set(building_policies)
    elif version_status is VersionStatus.EXTRACTED:
        candidates = {
            index_id
            for index_id, policy in building_policies.items()
            if policy not in policy_versions
        }
    else:
        return frozenset()
    return frozenset(candidates - set(indexed_index_ids))


def is_publishable_completed(
    *,
    document_status: DocumentStatus,
    version_status: VersionStatus,
    version_number: int,
    published_number: int | None,
    indexation_status: IndexationStatus,
    index_status: IndexStatus,
) -> bool:
    """Une publication perdue : tout ce que ``PublishDocumentVersion``
    vérifierait (§8 point 3, I5, I9) est satisfait."""
    return (
        document_status is DocumentStatus.ACTIVE
        and version_status is VersionStatus.EXTRACTED
        and (published_number is None or version_number > published_number)
        and indexation_status is IndexationStatus.COMPLETED
        and index_status is IndexStatus.ACTIVE
    )


# --- items FAILED (O14) ---------------------------------------------------------------


class FailedItemAction(StrEnum):
    IGNORE = "IGNORE"
    # Le hook d'échec n'a pas abouti : l'échec métier est écrit à sa place,
    # l'item n'est jamais rouvert (seul RetryIngestion rejoue).
    FAIL_VERSION = "FAIL_VERSION"
    FAIL_INDEXATION = "FAIL_INDEXATION"
    # Aucun hook métier ne rejouerait une purge : réouverture de l'item.
    REOPEN_PURGE = "REOPEN_PURGE"
    # Réouvertures épuisées : la purge attend un rejeu explicite.
    KEEP_FAILED_PURGE = "KEEP_FAILED_PURGE"


def decide_failed_item(
    kind: str,
    *,
    version_status: VersionStatus | None = None,
    indexation_status: IndexationStatus | None = None,
    document_status: DocumentStatus | None = None,
    purged: bool | None = None,
    live_work: bool = False,
    reopen_count: int = 0,
    max_reopenings: int | None = None,
) -> FailedItemAction:
    """``live_work`` : un autre item du même travail est encore en attente ou
    en cours, l'état métier lui appartient. ``max_reopenings`` : borne des
    réouvertures automatiques d'une purge (``None`` : aucune)."""
    if live_work:
        return FailedItemAction.IGNORE
    if kind == "extract_version" and version_status is VersionStatus.RECEIVED:
        return FailedItemAction.FAIL_VERSION
    if (
        kind in {"index_version", "rechunk_version"}
        and indexation_status is IndexationStatus.PENDING
    ):
        return FailedItemAction.FAIL_INDEXATION
    if (
        kind == "purge_document"
        and document_status is DocumentStatus.WITHDRAWN
        and purged is False
    ):
        if max_reopenings is not None and reopen_count >= max_reopenings:
            return FailedItemAction.KEEP_FAILED_PURGE
        return FailedItemAction.REOPEN_PURGE
    # Les autres kinds (sync_visibility, reconcile_knowledge_base…) : le
    # symptôme est corrigé par les autres règles, ou au cycle suivant.
    return FailedItemAction.IGNORE
