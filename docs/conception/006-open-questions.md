# 006 — Questions ouvertes et séquencement

## Modélisation métier à trancher

1. `Rubric` : critères, versions, QCM unique/multiple, réponses libres, pénalités et cas sans options incorrectes.
2. Questions catalogue vs questions générées. La provenance documentaire est fournie par Knowledge Base (`RetrievedPassage` et `Citation`, ADR 0007) ; reste à décider comment une question générée la fige dans `QuestionSnapshot`.
3. Progression : correction immédiate, différée, traitement des AssessmentFailed.
4. Taille de TrainingSession ; InterviewTurn comme agrégat distinct pour longues séances ?
5. Reassessment et politique de versions/reprises.
6. Révisions espacées et ownership futur de Learning Progress.

## Techniques proposées (non validées)

- FastAPI, SQLAlchemy Async, Alembic, PostgreSQL et ~~pgvector~~ — pgvector remplacé par Qdrant comme moteur vectoriel principal, voir [ADR 0006](../decisions/0006-technical-bootstrap.md) (extension disponible dans l'image, non activée).
- Celery + RabbitMQ, outbox/inbox, jobs de reconciliation.
- LiteLLM, providers Groq/Cerebras/Gemini/OpenRouter, free-only par défaut.
- ~~SentenceTransformers local pour embeddings~~ — décidé pour Knowledge Base par l'[ADR 0008](../decisions/0008-custom-pipeline-docling-extraction.md) ; profil initial révisable ([ADR 0011](../decisions/0011-embedding-profile-search-index-blue-green.md)).
- Polling V1 ou SSE pour retour des évaluations.

## Knowledge Base

Les questions ouvertes de Knowledge Base sont tenues **uniquement** dans [knowledge-base.md §16](bounded-contexts/knowledge-base.md#16-points-ouverts), pour éviter une liste dupliquée.

La stack ne devient formellement décidée qu'après acceptation d'un ADR. Les choix retenus pour le socle technique de la Phase 1 (asyncpg, PostgreSQL 18 sans pgvector actif, Qdrant, RabbitMQ sans Redis, result backend `rpc://`) sont consignés dans l'[ADR 0006](../decisions/0006-technical-bootstrap.md), statut Proposed.
