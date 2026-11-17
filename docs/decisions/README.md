# Architecture Decision Records

| ADR | Sujet | Statut |
|---|---|---|
| [0001](0001-two-bounded-contexts.md) | Interview Training / Assessment séparés | Accepted |
| [0002](0002-event-choreography-v1.md) | Chorégraphie événementielle V1 | Accepted |
| [0003](0003-modular-monolith-clean-architecture.md) | Monolithe modulaire / CA, conventions de [005](../conception/005-project-structure.md) | Accepted |
| [0004](0004-reliable-messaging.md) | Outbox, inbox, leases, reconciliation ; complément work queue durable et acquittement | Complément *work queue* Accepted ; outbox/inbox Proposed |
| [0005](0005-multi-llm-fallback.md) | Routage multi-LLM | Proposed |
| [0006](0006-technical-bootstrap.md) | Socle technique Phase 1 (stack, Qdrant, RabbitMQ sans Redis, readiness, Alembic) | Proposed |
| [0007](0007-knowledge-base-bounded-context.md) | Bounded Context Knowledge Base, contrat `KnowledgeSearch` | Accepted |
| [0008](0008-custom-pipeline-docling-extraction.md) | Pipeline personnalisé, extraction Docling, SentenceTransformers | Accepted (installation : Proposed) |
| [0009](0009-pedagogical-chunking-knowledge-units.md) | Chunking pédagogique par KnowledgeUnit | Accepted (paramètres : Proposed) |
| [0010](0010-postgresql-source-of-truth-qdrant-derived-index.md) | PostgreSQL source de vérité, Qdrant index dérivé, visibilité | Accepted (paramètres de recherche : Proposed) |
| [0011](0011-embedding-profile-search-index-blue-green.md) | EmbeddingProfile, SearchIndex, migration blue/green | Accepted ; profil initial révisable ; barrière Proposed |
| [0012](0012-original-document-storage-and-purge.md) | Stockage des originaux et purge | Accepted (rétention, doublons : Proposed / Open) |

Statuts : Proposed, Accepted, Superseded, Rejected. Un statut partiel indique
explicitement la partie de l'ADR concernée. Une décision Accepted n'est jamais
réécrite : elle est remplacée par un nouvel ADR qui la déclare Superseded.
Le profil d'embeddings définitif fera l'objet d'un nouvel ADR après le
benchmark S9 de Knowledge Base.
