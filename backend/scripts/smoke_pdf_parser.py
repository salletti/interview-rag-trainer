"""Test de fumée du parseur PDF dans l'image (revue S6, I3) : ``make
smoke-pdf-image``.

Les tests d'intégration tournent sur l'hôte : un rendu des pages propre au
conteneur (polices système absentes, rendues en rectangles) n'y serait pas vu.
Sans les polices de l'image, le modèle de layout ne reconnaît plus ni titres
ni code. Ce script convertit un PDF de fixtures avec ``DoclingPdfParser`` et
échoue (code 1) s'il n'en sort pas au moins un ``Heading`` et un
``CodeBlock``.

Usage : ``python scripts/smoke_pdf_parser.py <fichier.pdf>``.
"""

from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

from knowledge_base.adapters.gateway.parsing.docling_pdf_parser import (
    DoclingPdfParser,
)
from knowledge_base.domain.value_objects.structured_document import (
    CodeBlock,
    Heading,
)

REQUIRED = (Heading, CodeBlock)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write(f"usage : {argv[0]} <fichier.pdf>\n")
        return 2
    path = Path(argv[1])
    started = time.perf_counter()
    blocks = DoclingPdfParser().parse(path.read_bytes(), "application/pdf", path.name)
    elapsed = time.perf_counter() - started
    counts = Counter(type(block).__name__ for block in blocks.blocks)
    sys.stdout.write(
        f"{path.name} : {len(blocks.blocks)} blocs en {elapsed:.1f} s "
        f"({', '.join(f'{name} {n}' for name, n in sorted(counts.items()))})\n"
    )
    missing = [kind.__name__ for kind in REQUIRED if not counts[kind.__name__]]
    if missing:
        sys.stderr.write(
            f"ÉCHEC : aucun bloc {', '.join(missing)} ; polices de rendu des "
            "pages absentes de l'image ? (backend/Dockerfile, S6)\n"
        )
        return 1
    sys.stdout.write("OK : titres et blocs de code reconnus\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
