"""Décisions de réconciliation, pures (S8 ; knowledge-base.md §7.3 condition 6,
§9, §10, §11 ; roadmap S8). Aucune I/O : des faits en entrée, une décision en
sortie.

API figée (``knowledge_base.domain.services.reconciliation``) :

- ``PointFacts(document_id, version_id, visible)`` : un point lu dans Qdrant.
- ``DocumentFacts(status: DocumentStatus, purged: bool, versions:
  Mapping[UUID, VersionStatus])`` : l'état SQL du document, relu APRÈS le
  parcours de Qdrant (un point écrit après la lecture SQL serait sinon pris
  pour un orphelin).
- ``judge_point(point, document | None) -> PointVerdict`` avec
  ``PointVerdict`` = ``OK`` | ``ORPHAN`` (à supprimer) | ``RESYNC`` (rejouer
  ``sync_visibility``) | ``REPURGE`` (rejouer ``purge_document``).
- ``plan_point_corrections(points, documents) -> PointCorrections(delete:
  frozenset[PointSelector], resync: frozenset[UUID], repurge: frozenset[UUID])``.
- ``compare_manifest(expected: IndexManifest, recorded_digest: str, found:
  Mapping[UUID, str]) -> ManifestDrift | None`` (``missing``, ``different``,
  ``digest_changed``).
- ``indexes_missing_indexation(...)``, ``is_publishable_completed(...)``,
  ``decide_failed_item(...) -> FailedItemAction``.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from knowledge_base.domain.entities.indexation import IndexationStatus
from knowledge_base.domain.entities.knowledge_corpus import IndexStatus
from knowledge_base.domain.entities.knowledge_document import (
    DocumentStatus,
    VersionStatus,
)
from knowledge_base.domain.services.reconciliation import (
    DocumentFacts,
    FailedItemAction,
    ManifestDrift,
    PointCorrections,
    PointFacts,
    PointVerdict,
    compare_manifest,
    decide_failed_item,
    indexes_missing_indexation,
    is_publishable_completed,
    judge_point,
    plan_point_corrections,
)
from knowledge_base.domain.value_objects.index_manifest import IndexManifest
from knowledge_base.domain.value_objects.indexed_point import PointSelector

DOC = UUID(int=1)
V1, V2, V3 = UUID(int=11), UUID(int=12), UUID(int=13)
A = DocumentStatus.ACTIVE
W = DocumentStatus.WITHDRAWN
S = VersionStatus


def doc(
    status: DocumentStatus = A,
    *,
    purged: bool = False,
    **versions: VersionStatus,
) -> DocumentFacts:
    ids = {"v1": V1, "v2": V2, "v3": V3}
    by_id = {ids[name]: s for name, s in versions.items()}
    return DocumentFacts(status=status, purged=purged, versions=by_id)


def point(version: UUID = V1, *, visible: bool = False) -> PointFacts:
    return PointFacts(document_id=DOC, version_id=version, visible=visible)


# --- judge_point ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("document", "visible", "verdict"),
    [
        # document inconnu : le point n'a pas de propriétaire
        (None, False, PointVerdict.ORPHAN),
        (None, True, PointVerdict.ORPHAN),
        # version inconnue d'un document connu
        (doc(v2=S.PUBLISHED), False, PointVerdict.ORPHAN),
        # versions terminales : ne seront plus jamais publiées
        (doc(v1=S.SUPERSEDED, v2=S.PUBLISHED), False, PointVerdict.ORPHAN),
        (doc(v1=S.SUPERSEDED, v2=S.PUBLISHED), True, PointVerdict.ORPHAN),
        (doc(v1=S.DISCARDED), False, PointVerdict.ORPHAN),
        # version publiée d'un document actif : le drapeau doit être vrai
        (doc(v1=S.PUBLISHED), True, PointVerdict.OK),
        (doc(v1=S.PUBLISHED), False, PointVerdict.RESYNC),
        # version en cours d'indexation ou à publier : invisible, c'est normal
        (doc(v1=S.EXTRACTED), False, PointVerdict.OK),
        (doc(v1=S.RECEIVED), False, PointVerdict.OK),
        (doc(v1=S.FAILED), False, PointVerdict.OK),
        # document retiré : la purge en attente est légitime, pas un écart
        (doc(W, v1=S.PUBLISHED), True, PointVerdict.OK),
        (doc(W, v1=S.PUBLISHED), False, PointVerdict.OK),
        # document retiré ET purgé : tentative zombie, rejouer la purge
        (doc(W, purged=True, v1=S.PUBLISHED), False, PointVerdict.REPURGE),
        (doc(W, purged=True, v1=S.EXTRACTED), False, PointVerdict.REPURGE),
    ],
    ids=lambda value: str(getattr(value, "value", value))[:28],
)
def test_a_point_is_judged_against_the_sql_state_of_its_document(
    document: DocumentFacts | None, visible: bool, verdict: PointVerdict
) -> None:
    assert judge_point(point(V1, visible=visible), document) is verdict


def test_a_hidden_published_point_is_not_confused_with_an_obsolete_one() -> None:
    document = doc(v1=S.SUPERSEDED, v2=S.PUBLISHED)

    assert judge_point(point(V2, visible=False), document) is PointVerdict.RESYNC
    assert judge_point(point(V1, visible=False), document) is PointVerdict.ORPHAN


# --- plan_point_corrections ----------------------------------------------------------


def test_orphans_are_grouped_into_one_selector_per_version() -> None:
    points = [point(V1), point(V1), point(V1), point(V2)]
    documents = {DOC: doc(v1=S.DISCARDED, v2=S.SUPERSEDED, v3=S.PUBLISHED)}

    plan = plan_point_corrections(points, documents)

    assert plan == PointCorrections(
        delete=frozenset({PointSelector(DOC, V1), PointSelector(DOC, V2)}),
        resync=frozenset(),
        repurge=frozenset(),
    )


def test_the_points_of_an_unknown_document_are_deleted_by_document() -> None:
    plan = plan_point_corrections([point(V1), point(V2)], {})

    assert plan.delete == frozenset({PointSelector(DOC)})


def test_the_points_of_an_unknown_version_are_deleted_by_version_only() -> None:
    plan = plan_point_corrections([point(V3)], {DOC: doc(v1=S.PUBLISHED)})

    assert plan.delete == frozenset({PointSelector(DOC, V3)})


def test_hidden_published_points_request_one_resync_per_document() -> None:
    other = UUID(int=2)
    points = [
        point(V1),
        point(V1),
        PointFacts(document_id=other, version_id=V2, visible=False),
    ]
    documents = {
        DOC: doc(v1=S.PUBLISHED),
        other: DocumentFacts(A, False, {V2: S.PUBLISHED}),
    }

    plan = plan_point_corrections(points, documents)

    assert plan.resync == frozenset({DOC, other})
    assert plan.delete == frozenset()
    assert plan.repurge == frozenset()


def test_zombie_points_of_a_purged_document_request_one_purge() -> None:
    documents = {DOC: doc(W, purged=True, v1=S.PUBLISHED, v2=S.EXTRACTED)}

    plan = plan_point_corrections([point(V1), point(V2), point(V2)], documents)

    assert plan.repurge == frozenset({DOC})
    assert plan.delete == frozenset()


def test_a_consistent_index_needs_no_correction() -> None:
    documents = {DOC: doc(v1=S.PUBLISHED, v2=S.EXTRACTED)}
    points = [point(V1, visible=True), point(V2, visible=False)]

    assert plan_point_corrections(points, documents) == PointCorrections(
        delete=frozenset(), resync=frozenset(), repurge=frozenset()
    )


def test_a_withdrawn_document_waiting_for_its_purge_is_left_alone() -> None:
    documents = {DOC: doc(W, purged=False, v1=S.PUBLISHED)}

    assert plan_point_corrections([point(V1, visible=True)], documents) == (
        PointCorrections(frozenset(), frozenset(), frozenset())
    )


# --- compare_manifest ----------------------------------------------------------------

P1, P2, P3 = UUID(int=101), UUID(int=102), UUID(int=103)
H1, H2, H3 = "a" * 64, "b" * 64, "c" * 64
EXPECTED = IndexManifest.of([(P1, H1), (P2, H2), (P3, H3)])


def test_a_manifest_that_matches_qdrant_and_its_digest_is_not_a_drift() -> None:
    found = {P1: H1, P2: H2, P3: H3}

    assert compare_manifest(EXPECTED, EXPECTED.digest, found) is None


def test_a_missing_point_is_a_drift() -> None:
    drift = compare_manifest(EXPECTED, EXPECTED.digest, {P1: H1, P3: H3})

    assert drift == ManifestDrift(
        missing=frozenset({P2}), different=frozenset(), digest_changed=False
    )


def test_a_modified_chunk_hash_is_a_drift() -> None:
    drift = compare_manifest(EXPECTED, EXPECTED.digest, {P1: H1, P2: "d" * 64, P3: H3})

    assert drift == ManifestDrift(
        missing=frozenset(), different=frozenset({P2}), digest_changed=False
    )


def test_a_recorded_digest_that_no_longer_matches_the_chunks_is_a_drift() -> None:
    # Les points sont conformes aux chunks actuels, mais ce n'est plus ce que
    # l'indexation a enregistré comme vérifié : I8 ne tient plus.
    drift = compare_manifest(EXPECTED, "0" * 64, {P1: H1, P2: H2, P3: H3})

    assert drift == ManifestDrift(
        missing=frozenset(), different=frozenset(), digest_changed=True
    )


def test_every_kind_of_drift_is_reported_together() -> None:
    drift = compare_manifest(EXPECTED, "0" * 64, {P2: "d" * 64})

    assert drift == ManifestDrift(
        missing=frozenset({P1, P3}),
        different=frozenset({P2}),
        digest_changed=True,
    )


# --- versions EXTRACTED sans indexation ---------------------------------------------

I_ACTIVE, I_BUILDING = UUID(int=201), UUID(int=202)


@pytest.mark.parametrize(
    ("document", "version", "indexed", "missing"),
    [
        (A, S.EXTRACTED, set(), {I_ACTIVE, I_BUILDING}),
        (A, S.EXTRACTED, {I_ACTIVE}, {I_BUILDING}),
        (A, S.EXTRACTED, {I_ACTIVE, I_BUILDING}, set()),
        # un autre état d'indexation (FAILED, CANCELLED…) est un fait existant :
        # il relève des échecs, pas de « sans indexation »
        (A, S.PUBLISHED, set(), set()),
        (A, S.RECEIVED, set(), set()),
        (A, S.FAILED, set(), set()),
        (A, S.DISCARDED, set(), set()),
        (A, S.SUPERSEDED, set(), set()),
        (W, S.EXTRACTED, set(), set()),
    ],
)
def test_only_an_extracted_version_of_an_active_document_misses_indexations(
    document: DocumentStatus,
    version: VersionStatus,
    indexed: set[UUID],
    missing: set[UUID],
) -> None:
    assert indexes_missing_indexation(
        document_status=document,
        version_status=version,
        live_index_ids={I_ACTIVE, I_BUILDING},
        indexed_index_ids=indexed,
    ) == frozenset(missing)


# --- indexations COMPLETED non publiées ---------------------------------------------


def publishable(**overrides: object) -> bool:
    facts: dict[str, object] = {
        "document_status": A,
        "version_status": S.EXTRACTED,
        "version_number": 2,
        "published_number": 1,
        "indexation_status": IndexationStatus.COMPLETED,
        "index_status": IndexStatus.ACTIVE,
    }
    facts.update(overrides)
    return is_publishable_completed(**facts)  # type: ignore[arg-type]


def test_a_completed_version_newer_than_the_published_one_is_publishable() -> None:
    assert publishable()
    assert publishable(published_number=None)


@pytest.mark.parametrize(
    "overrides",
    [
        {"document_status": W},
        {"version_status": S.PUBLISHED},
        {"version_status": S.DISCARDED},
        {"version_status": S.RECEIVED},
        {"version_number": 1},  # I5 : pas plus ancienne ni égale à la publiée
        {"version_number": 0},
        {"indexation_status": IndexationStatus.PENDING},
        {"indexation_status": IndexationStatus.FAILED},
        {"indexation_status": IndexationStatus.CANCELLED},
        {"index_status": IndexStatus.BUILDING},  # I9 : l'index ACTIVE seulement
        {"index_status": IndexStatus.RETIRED},
    ],
    ids=lambda overrides: ",".join(
        f"{k}={getattr(v, 'value', v)}" for k, v in overrides.items()
    ),
)
def test_publication_is_never_proposed_when_an_invariant_would_refuse_it(
    overrides: dict[str, object],
) -> None:
    assert not publishable(**overrides)


# --- items FAILED sans état métier (O14) --------------------------------------------


@pytest.mark.parametrize(
    ("kind", "facts", "action"),
    [
        # le hook d'échec n'a pas abouti : l'agrégat attend encore
        (
            "extract_version",
            {"version_status": S.RECEIVED},
            FailedItemAction.FAIL_VERSION,
        ),
        ("extract_version", {"version_status": S.FAILED}, FailedItemAction.IGNORE),
        ("extract_version", {"version_status": S.DISCARDED}, FailedItemAction.IGNORE),
        ("extract_version", {"version_status": S.EXTRACTED}, FailedItemAction.IGNORE),
        ("extract_version", {}, FailedItemAction.IGNORE),
        (
            "index_version",
            {"indexation_status": IndexationStatus.PENDING},
            FailedItemAction.FAIL_INDEXATION,
        ),
        (
            "index_version",
            {"indexation_status": IndexationStatus.FAILED},
            FailedItemAction.IGNORE,
        ),
        (
            "index_version",
            {"indexation_status": IndexationStatus.CANCELLED},
            FailedItemAction.IGNORE,
        ),
        ("index_version", {}, FailedItemAction.IGNORE),
        # purge : aucun hook métier, rien ne la rejouera sans la réconciliation
        (
            "purge_document",
            {"document_status": W, "purged": False},
            FailedItemAction.REOPEN_PURGE,
        ),
        (
            "purge_document",
            {"document_status": W, "purged": True},
            FailedItemAction.IGNORE,
        ),
        (
            "purge_document",
            {"document_status": A, "purged": False},
            FailedItemAction.IGNORE,
        ),
        ("purge_document", {}, FailedItemAction.IGNORE),
        # les autres kinds : le symptôme est corrigé par les autres règles
        ("sync_visibility", {}, FailedItemAction.IGNORE),
        ("reconcile_knowledge_base", {}, FailedItemAction.IGNORE),
        ("kind_inconnu", {}, FailedItemAction.IGNORE),
    ],
)
def test_a_failed_item_is_acted_on_only_when_its_aggregate_never_heard_of_it(
    kind: str, facts: dict[str, object], action: FailedItemAction
) -> None:
    assert decide_failed_item(kind, **facts) is action  # type: ignore[arg-type]
