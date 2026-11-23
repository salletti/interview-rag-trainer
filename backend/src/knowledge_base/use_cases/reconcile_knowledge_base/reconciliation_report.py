"""Ce qu'un cycle de réconciliation a trouvé (S8)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from knowledge_base.domain.services.reconciliation import DiscrepancyKind


@dataclass(frozen=True)
class ReconciliationReport:
    """Décompte par type d'écart (unités : ``DiscrepancyKind``). Un écart
    CORRIGÉ pendant le cycle compte : le rapport dit ce qui a été trouvé, pas
    ce qui reste ; c'est le cycle suivant qui dit « propre »."""

    counts: Mapping[DiscrepancyKind, int] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def count(self, kind: DiscrepancyKind) -> int:
        return self.counts.get(kind, 0)

    @property
    def clean(self) -> bool:
        return not any(count > 0 for count in self.counts.values())

    def __str__(self) -> str:
        found = ", ".join(
            f"{kind.value}={count}"
            for kind, count in sorted(self.counts.items())
            if count > 0
        )
        return found or "aucun écart"
