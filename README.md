# Interview RAG Trainer

Application personnelle d'entraînement aux entretiens techniques (Python, FastAPI, DDD, RAG et Agentic AI), avec correction des réponses, suivi de progression et connaissances RAG.

> **Statut : conception uniquement.** Aucun backend, worker ou RAG fonctionnel n'est encore implémenté.

## Documentation

- [Index](docs/README.md)
- [Contexte métier](docs/context/domain.md)
- [Bounded Contexts](docs/conception/001-bounded-contexts.md)
- [Flux et événements](docs/conception/002-event-flows.md)
- [Agrégats et invariants](docs/conception/003-aggregates-and-invariants.md)
- [Contrats d'intégration](docs/conception/004-integration-contracts.md)
- [Structure Clean Architecture](docs/conception/005-project-structure.md)
- [Questions ouvertes](docs/conception/006-open-questions.md)
- [ADR](docs/decisions/README.md)

Architecture envisagée : monolithe modulaire, FastAPI, PostgreSQL/pgvector, RabbitMQ/Celery, routage multi-LLM (Groq, Cerebras, Gemini, OpenRouter).

Structure inspirée de `salletti/trading-agent-ai` (`domain`, `use_cases`, `adapters`, `contracts`, `frameworks` et `runtime`).
