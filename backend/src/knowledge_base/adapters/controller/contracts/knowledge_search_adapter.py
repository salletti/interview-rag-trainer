"""Implémentation du contrat public ``KnowledgeSearch`` (knowledge-base.md §14)
sur la query ``SearchKnowledge`` : traduction des types du contrat vers ceux du
cas d'usage, et retour. Aucune décision ici."""

from __future__ import annotations

from knowledge_base.contracts.providers import (
    KnowledgeSearchUnavailableError,
    UnavailabilityReason,
)
from knowledge_base.contracts.result import (
    Citation,
    KnowledgeQuery,
    KnowledgeSearchResult,
    RetrievedPassage,
)
from knowledge_base.domain.value_objects.citation import Citation as DomainCitation
from knowledge_base.use_cases.search_knowledge.errors import (
    NoActiveSearchIndexError,
    SearchProfileMismatchError,
)
from knowledge_base.use_cases.search_knowledge.search_knowledge_command import (
    SearchKnowledge,
)
from knowledge_base.use_cases.search_knowledge.search_knowledge_handler import (
    SearchKnowledgeHandler,
)
from knowledge_base.use_cases.search_knowledge.search_knowledge_result import (
    FoundPassage,
)


class KnowledgeSearchAdapter:
    def __init__(self, handler: SearchKnowledgeHandler) -> None:
        self._handler = handler

    async def search(self, query: KnowledgeQuery) -> KnowledgeSearchResult:
        command = SearchKnowledge(
            text=query.text,
            limit=query.limit,
            language=query.language,
            topics=tuple(query.topics),
            kinds=tuple(query.kinds),
            document_ids=tuple(query.document_ids),
        )
        try:
            result = await self._handler.handle(command)
        except NoActiveSearchIndexError as error:
            raise KnowledgeSearchUnavailableError(
                UnavailabilityReason.NO_ACTIVE_SEARCH_INDEX, str(error)
            ) from error
        except SearchProfileMismatchError as error:
            raise KnowledgeSearchUnavailableError(
                UnavailabilityReason.SEARCH_PROFILE_MISMATCH, str(error)
            ) from error
        return KnowledgeSearchResult(
            passages=tuple(_passage(p) for p in result.passages),
            complete=result.complete,
            index_id=result.index_id,
        )


def _passage(found: FoundPassage) -> RetrievedPassage:
    return RetrievedPassage(
        unit_id=found.unit_id,
        unit_type=found.unit_type.value,
        question=found.question,
        text=found.text,
        truncated=found.truncated,
        matched_chunk_ids=found.matched_chunk_ids,
        score=found.score,
        citation=_citation(found.citation),
    )


def _citation(citation: DomainCitation) -> Citation:
    return Citation(
        document_id=citation.document_id,
        document_title=citation.document_title,
        version_id=citation.version_id,
        version_number=citation.version_number,
        section_path=citation.section_path.parts,
        page_start=citation.page_start,
        page_end=citation.page_end,
        source_filename=citation.source_filename,
    )
