"""Port des dépôts de ``RecordExtraction.apply`` et ``fail``, liés à une
transaction ouverte par la work queue (S0 §5).

Depuis S4, la même transaction crée les ``Indexation`` de la version et leurs
items ``index_version`` (knowledge-base.md §6, §13) : elle expose aussi le
corpus, les indexations et la work queue.

Depuis S9, le rechunking d'une version pour un index d'une autre politique
(``RechunkVersion``) écrit un second lot dans une transaction de même forme ;
il relit d'abord les chunks du lot visé, qu'il n'écrit qu'une fois (I7).
"""

from __future__ import annotations

from typing import Protocol

from knowledge_base.domain.providers.chunk_reader import ChunkReader
from knowledge_base.domain.providers.extraction_repository import (
    ExtractionRepository,
)
from knowledge_base.domain.providers.indexing_transaction import (
    IndexingTransaction,
)


class ExtractionTransaction(IndexingTransaction, Protocol):
    """Dépôts liés à la transaction de finalisation (ou d'échec) en cours ; la
    transaction appartient à l'appelant."""

    @property
    def extractions(self) -> ExtractionRepository: ...


class RechunkTransaction(ExtractionTransaction, Protocol):
    """Celle de l'extraction, plus la lecture des chunks d'un lot."""

    @property
    def chunks(self) -> ChunkReader: ...
