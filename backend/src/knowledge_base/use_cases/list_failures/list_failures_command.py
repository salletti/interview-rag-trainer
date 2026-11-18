from __future__ import annotations

from dataclasses import dataclass
from typing import Final

MAX_FAILURES_LIMIT: Final = 500


@dataclass(frozen=True)
class ListFailures:
    """Query : les ``limit`` items ``FAILED`` les plus récents."""

    limit: int = 100

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= MAX_FAILURES_LIMIT:
            raise ValueError(
                f"limit doit être entre 1 et {MAX_FAILURES_LIMIT} : {self.limit}"
            )
