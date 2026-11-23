"""Représentation neutre d'un document extrait (knowledge-base.md §2, §3.5 ;
ADR 0008 §2) : la couche anti-corruption de chaque parseur traduit sa sortie
dans ces blocs, et le segmenteur ne connaît qu'eux.

``page`` vaut ``None`` quand le format n'a pas de pages (Markdown, TXT).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


@dataclass(frozen=True)
class Heading:
    level: int
    text: str
    page: int | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.level <= 6:
            raise ValueError(f"level de titre hors de 1..6 : {self.level}")


@dataclass(frozen=True)
class Paragraph:
    text: str
    page: int | None = None


@dataclass(frozen=True)
class CodeBlock:
    text: str
    language: str | None = None
    page: int | None = None


@dataclass(frozen=True)
class Table:
    """Texte tabulaire, une ligne par rangée."""

    text: str
    page: int | None = None


@dataclass(frozen=True)
class ListBlock:
    items: tuple[str, ...]
    page: int | None = None


Block = Heading | Paragraph | CodeBlock | Table | ListBlock


@dataclass(frozen=True)
class StructuredDocument:
    """``metadata`` : le front-matter du document (``clé: valeur``), figé à la
    construction."""

    blocks: tuple[Block, ...]
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


def render_block(block: Block) -> str:
    match block:
        case Heading(level=level, text=text):
            return f"{'#' * level} {text}"
        case CodeBlock(text=text, language=language):
            return f"```{language or ''}\n{text}\n```"
        case ListBlock(items=items):
            return "\n".join(f"- {item}" for item in items)
        case Paragraph(text=text) | Table(text=text):
            return text


def render_blocks(blocks: Iterable[Block]) -> str:
    """Texte des blocs, séparés par une ligne vide : c'est ce texte qui est
    compté en tokens et embarqué."""
    return "\n\n".join(render_block(block) for block in blocks)
