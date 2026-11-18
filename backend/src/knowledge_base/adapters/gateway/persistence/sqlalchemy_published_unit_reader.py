"""Contrôle SQL final de la recherche (knowledge-base.md §11, §12 étapes 4
et 6 ; ADR 0010 §4, §6 ; I12).

Deux requêtes dans une session courte, quel que soit le nombre d'unités :

1. les unités demandées dont la version est ``PUBLISHED`` et le document
   ``ACTIVE`` (clé primaire des unités, jointures par clé primaire), avec de
   quoi construire leur citation ;
2. les chunks de ces seules unités, dans l'ordre de lecture (index unique
   ``(unit_id, ordinal_in_unit)``).

La citation est celle de l'unité : titre courant du document, numéro et nom
de fichier de la version, chemin de sections et pages de l'unité.

``token_count`` d'une ``PublishedUnit`` : celui des blocs (calculé à
l'extraction avec le tokenizer de la politique) plus la ligne de question
comptée par ``question_counter``. En production, ``ApproximateTokenCounter``,
qui majore le tokenizer XLM-R sans charger le modèle : quelques tokens d'excès
sur une question, sans conséquence sur un budget de passage.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import Row, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_base.domain.providers.token_counter import TokenCounter
from knowledge_base.domain.services.chunk_planner import QUESTION_LABEL
from knowledge_base.domain.value_objects.citation import Citation
from knowledge_base.domain.value_objects.knowledge_unit import UnitType
from knowledge_base.domain.value_objects.published_unit import (
    PublishedUnit,
    UnitChunk,
)
from knowledge_base.domain.value_objects.section_path import SectionPath
from knowledge_base.frameworks.sqlalchemy.tables import (
    document_chunks,
    document_versions,
    documents,
    knowledge_units,
)

_u = knowledge_units.c
_v = document_versions.c
_d = documents.c
_ch = document_chunks.c


class SqlAlchemyPublishedUnitReader:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        question_counter: TokenCounter,
    ) -> None:
        self._session_factory = session_factory
        self._question_counter = question_counter

    async def read_published(
        self, unit_ids: Sequence[UUID]
    ) -> Mapping[UUID, PublishedUnit]:
        if not unit_ids:
            return {}
        async with self._session_factory() as session:
            units = (
                await session.execute(
                    select(
                        _u.id,
                        _u.unit_type,
                        _u.question_text,
                        _u.section_path,
                        _u.page_start,
                        _u.page_end,
                        _u.token_count,
                        _u.text,
                        _v.id.label("version_id"),
                        _v.number,
                        _v.filename,
                        _d.id.label("document_id"),
                        _d.title,
                    )
                    .join(document_versions, _v.id == _u.version_id)
                    .join(documents, _d.id == _v.document_id)
                    .where(
                        _u.id.in_(unit_ids),
                        _v.status == "PUBLISHED",
                        _d.status == "ACTIVE",
                    )
                )
            ).all()
            if not units:
                return {}
            chunk_rows = (
                await session.execute(
                    select(
                        _ch.unit_id,
                        _ch.id,
                        _ch.ordinal_in_unit,
                        _ch.contextual_text,
                        _ch.token_count,
                    )
                    .where(_ch.unit_id.in_([row.id for row in units]))
                    .order_by(_ch.unit_id, _ch.ordinal_in_unit)
                )
            ).all()
        chunks: dict[UUID, list[UnitChunk]] = {}
        for chunk in chunk_rows:
            chunks.setdefault(chunk.unit_id, []).append(
                UnitChunk(
                    chunk_id=chunk.id,
                    ordinal_in_unit=chunk.ordinal_in_unit,
                    text=chunk.contextual_text,
                    token_count=chunk.token_count,
                )
            )
        return {
            row.id: self._published_unit(row, tuple(chunks.get(row.id, ())))
            for row in units
        }

    def _published_unit(
        self, row: Row[*tuple[Any, ...]], chunks: tuple[UnitChunk, ...]
    ) -> PublishedUnit:
        question_tokens = (
            0
            if row.question_text is None
            else self._question_counter.count(f"{QUESTION_LABEL}{row.question_text}")
        )
        return PublishedUnit(
            unit_id=row.id,
            unit_type=UnitType(row.unit_type),
            question_text=row.question_text,
            text=row.text,
            token_count=row.token_count + question_tokens,
            chunks=chunks,
            citation=Citation(
                document_id=row.document_id,
                document_title=row.title,
                version_id=row.version_id,
                version_number=row.number,
                section_path=SectionPath(tuple(row.section_path)),
                page_start=row.page_start,
                page_end=row.page_end,
                source_filename=row.filename,
            ),
        )
