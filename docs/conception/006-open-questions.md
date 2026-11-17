# 006 — Questions ouvertes et séquencement

## Modélisation métier à trancher

1. `Rubric` : critères, versions, QCM unique/multiple, réponses libres, pénalités et cas sans options incorrectes.
2. Questions catalogue vs questions générées ; provenance Knowledge Base / RAG.
3. Progression : correction immédiate, différée, traitement des AssessmentFailed.
4. Taille de TrainingSession ; InterviewTurn comme agrégat distinct pour longues séances ?
5. Reassessment et politique de versions/reprises.
6. Révisions espacées et ownership futur de Learning Progress.

## Techniques proposées (non validées)

- FastAPI, SQLAlchemy Async, Alembic, PostgreSQL et ~~pgvector~~ — pgvector remplacé par Qdrant comme moteur vectoriel principal, voir [ADR 0006](../decisions/0006-technical-bootstrap.md) (extension disponible dans l'image, non activée).
- Celery + RabbitMQ, outbox/inbox, jobs de reconciliation.
- LiteLLM, providers Groq/Cerebras/Gemini/OpenRouter, free-only par défaut.
- SentenceTransformers local pour embeddings.
- Polling V1 ou SSE pour retour des évaluations.

La stack ne devient formellement décidée qu'après acceptation d'un ADR. Les choix retenus pour le socle technique de la Phase 1 (asyncpg, PostgreSQL 18 sans pgvector actif, Qdrant, RabbitMQ sans Redis, result backend `rpc://`) sont consignés dans l'[ADR 0006](../decisions/0006-technical-bootstrap.md), statut Proposed.
