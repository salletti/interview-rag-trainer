from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True)
class CleanStaleUploads:
    """``grace`` : âge en deçà duquel un temporaire de dépôt est conservé (un
    import en cours) ; celui des blobs orphelins (``KB_BLOB_ORPHAN_GRACE_SECONDS``)."""

    grace: timedelta
