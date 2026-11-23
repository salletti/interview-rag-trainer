"""Route de recherche de Knowledge Base (S5 ; knowledge-base.md §12, §14).

La route sert le contrat public ``KnowledgeSearch`` : l'API REST et les
autres contextes passent par la même porte. Elle traduit le corps JSON en
``KnowledgeQuery``, le résultat en JSON, et l'indisponibilité de la recherche
en 503 ``{code, message}``. Toute autre erreur (PostgreSQL ou Qdrant
injoignable) remonte : 500, jamais un faux « rien trouvé ».

Pas de ``from __future__ import annotations`` ici : FastAPI doit résoudre les
annotations des routes, qui référencent des dépendances définies localement.
"""

from collections.abc import Callable
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from knowledge_base.adapters.controller.rest.documents_router import PREFIX, TAGS
from knowledge_base.adapters.controller.rest.schemas import ErrorResponse
from knowledge_base.adapters.controller.rest.search_schemas import (
    CitationResponse,
    PassageResponse,
    SearchRequest,
    SearchResponse,
)
from knowledge_base.contracts.providers import (
    KnowledgeSearch,
    KnowledgeSearchUnavailableError,
)
from knowledge_base.contracts.result import (
    KnowledgeQuery,
    KnowledgeSearchResult,
    RetrievedPassage,
)

KnowledgeSearchFactory = Callable[[], KnowledgeSearch]

_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": ErrorResponse,
        "description": "Recherche indisponible : pas d'index actif "
        "(NO_ACTIVE_SEARCH_INDEX) ou profil d'embeddings incompatible "
        "(SEARCH_PROFILE_MISMATCH)",
    },
}


def add_search_routes(
    router: APIRouter, *, knowledge_search: KnowledgeSearchFactory
) -> None:
    """``knowledge_search`` est appelé à chaque requête (dépendance FastAPI)."""

    @router.post(
        f"{PREFIX}/search",
        tags=TAGS,
        response_model=SearchResponse,
        responses=_RESPONSES,
        summary="Rechercher des passages sourcés",
    )
    async def search(
        body: SearchRequest,
        searcher: Annotated[KnowledgeSearch, Depends(knowledge_search)],
    ) -> SearchResponse | JSONResponse:
        try:
            result = await searcher.search(_query(body))
        except KnowledgeSearchUnavailableError as error:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"code": error.reason.value, "message": str(error)},
            )
        return _response(result)


def _query(body: SearchRequest) -> KnowledgeQuery:
    return KnowledgeQuery(
        text=body.text,
        limit=body.limit,
        language=body.language,
        topics=tuple(body.topics),
        kinds=tuple(kind.value for kind in body.kinds),
        document_ids=tuple(body.document_ids),
    )


def _response(result: KnowledgeSearchResult) -> SearchResponse:
    return SearchResponse(
        index_id=result.index_id,
        complete=result.complete,
        passages=[_passage(p) for p in result.passages],
    )


def _passage(passage: RetrievedPassage) -> PassageResponse:
    citation = passage.citation
    return PassageResponse(
        unit_id=passage.unit_id,
        unit_type=passage.unit_type,
        question=passage.question,
        text=passage.text,
        truncated=passage.truncated,
        matched_chunk_ids=list(passage.matched_chunk_ids),
        score=passage.score,
        citation=CitationResponse(
            document_id=citation.document_id,
            document_title=citation.document_title,
            version_id=citation.version_id,
            version_number=citation.version_number,
            section_path=list(citation.section_path),
            page_start=citation.page_start,
            page_end=citation.page_end,
            source_filename=citation.source_filename,
        ),
    )
