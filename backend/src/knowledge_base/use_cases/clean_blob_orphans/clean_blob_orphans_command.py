from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True)
class CleanBlobOrphans:
    """``grace`` : âge en deçà duquel un blob non référencé est conservé (dépôt
    d'un import dont la transaction n'a pas encore commité, ADR 0012)."""

    grace: timedelta
