"""``ReconciliationReport`` : ce qu'un cycle a trouvé (S8).

API figée (``knowledge_base.use_cases.reconcile_knowledge_base.
reconciliation_report``) :

- ``ReconciliationReport(counts: Mapping[DiscrepancyKind, int])`` ;
  ``DiscrepancyKind`` (``knowledge_base.domain.services.reconciliation``) :
  ``COMPLETED_NOT_PUBLISHED``, ``EXTRACTED_WITHOUT_INDEXATION``,
  ``ORPHAN_POINTS``, ``MANIFEST_DRIFT``, ``HIDDEN_PUBLISHED_POINTS``,
  ``WITHDRAWN_DOCUMENT_POINTS``, ``FAILED_WORK_WITHOUT_STATE`` (O14),
  ``FAILED_PURGE``, ``QUARANTINED_BLOB`` (fichier laissé sous
  ``quarantine/`` par un crash : remis en place ou supprimé ; ajouté par la
  décision 9 de l'orchestrateur, postérieure aux tests rouges), ``ORPHAN_BLOB``,
  ``STALE_UPLOAD``, ``EXPIRED_IDEMPOTENCY_KEY``.
- ``count(kind) -> int`` : 0 pour un type absent. Unité du décompte : le
  nombre de POINTS pour ``ORPHAN_POINTS``, ``HIDDEN_PUBLISHED_POINTS`` et
  ``WITHDRAWN_DOCUMENT_POINTS`` ; le nombre d'entités (versions, indexations,
  items, fichiers, clés) pour les autres.
- ``clean`` : vrai si et seulement si aucun type n'a un décompte positif.
  Un écart CORRIGÉ pendant le cycle compte : le rapport dit ce qui a été
  trouvé, pas ce qui reste ; c'est le cycle suivant qui dit « propre ».
"""

from __future__ import annotations

import pytest

from knowledge_base.domain.services.reconciliation import DiscrepancyKind
from knowledge_base.use_cases.reconcile_knowledge_base.reconciliation_report import (
    ReconciliationReport,
)

EXPECTED_KINDS = {
    "COMPLETED_NOT_PUBLISHED",
    "EXTRACTED_WITHOUT_INDEXATION",
    "ORPHAN_POINTS",
    "MANIFEST_DRIFT",
    "HIDDEN_PUBLISHED_POINTS",
    "WITHDRAWN_DOCUMENT_POINTS",
    "FAILED_WORK_WITHOUT_STATE",
    "FAILED_PURGE",
    "QUARANTINED_BLOB",
    "ORPHAN_BLOB",
    "STALE_UPLOAD",
    "EXPIRED_IDEMPOTENCY_KEY",
}


def test_the_discrepancy_kinds_are_the_documented_ones() -> None:
    assert {kind.value for kind in DiscrepancyKind} == EXPECTED_KINDS
    assert all(kind.name == kind.value for kind in DiscrepancyKind)


def test_an_absent_kind_counts_zero() -> None:
    report = ReconciliationReport(counts={DiscrepancyKind.ORPHAN_BLOB: 2})

    assert report.count(DiscrepancyKind.ORPHAN_BLOB) == 2
    assert report.count(DiscrepancyKind.MANIFEST_DRIFT) == 0


def test_an_empty_report_is_clean() -> None:
    assert ReconciliationReport(counts={}).clean


def test_a_report_with_only_zero_counts_is_clean() -> None:
    assert ReconciliationReport(counts={DiscrepancyKind.ORPHAN_BLOB: 0}).clean


@pytest.mark.parametrize("kind", list(DiscrepancyKind))
def test_any_positive_count_makes_the_report_unclean(kind: DiscrepancyKind) -> None:
    assert not ReconciliationReport(counts={kind: 1}).clean
