"""Pages couvertes par une unité ou un chunk (knowledge-base.md §3.5)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Self


@dataclass(frozen=True)
class PageSpan:
    """Deux ``None`` (format sans pages, ou pages inconnues) ou
    ``1 <= start <= end``."""

    start: int | None
    end: int | None

    def __post_init__(self) -> None:
        validate_pages(self.start, self.end)

    @classmethod
    def covering(cls, pages: Iterable[int | None]) -> Self:
        """Plus petite et plus grande des pages connues ; inconnu si aucune."""
        known = [page for page in pages if page is not None]
        if not known:
            return cls(None, None)
        return cls(min(known), max(known))


def validate_pages(start: int | None, end: int | None) -> None:
    if start is None and end is None:
        return
    if start is None or end is None:
        raise ValueError(f"pages incomplètes : début {start}, fin {end}")
    if not 1 <= start <= end:
        raise ValueError(f"pages incohérentes : début {start}, fin {end}")
