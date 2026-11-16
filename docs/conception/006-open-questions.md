# 006 — Questions ouvertes et séquencement

## Modélisation métier à trancher

1. `Rubric` : critères, versions, QCM unique/multiple, réponses libres, pénalités et cas sans options incorrectes.
2. Questions catalogue vs questions générées ; provenance Knowledge Base / RAG.
3. Progression : correction immédiate, différée, traitement des AssessmentFailed.
4. Taille de TrainingSession ; InterviewTurn comme agrégat distinct pour longues séances ?
5. Reassessment et politique de versions/reprises.
6. Révisions espacées et ownership futur de Learning Progress.

## Techniques proposées (non validées)

- FastAPI, SQLAlchemy Async, Alembic, PostgreSQL et pgvector.
- Celery + RabbitMQ, outbox/inbox, jobs de reconciliation.
- LiteLLM, providers Groq/Cerebras/Gemini/OpenRouter, free-only par défaut.
- SentenceTransformers local pour embeddings.
- Polling V1 ou SSE pour retour des évaluations.

La stack ne devient formellement décidée qu'après acceptation d'un ADR.
