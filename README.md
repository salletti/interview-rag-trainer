# Interview RAG Trainer

Application personnelle d'entraînement aux entretiens techniques (Python, FastAPI, DDD, RAG et Agentic AI), avec correction des réponses, suivi de progression et connaissances RAG.

> **Statut : socle technique (Phase 1).** L'environnement complet démarre en une commande ; aucune fonctionnalité métier n'est encore implémentée.

## Démarrage rapide

```bash
docker compose up --build -d
```

- API : http://localhost:8010/docs — `GET /health/live`, `GET /health/ready`, `GET /api/v1/system/info`
- Frontend : http://localhost:5180
- RabbitMQ management : http://localhost:15672

Guide complet : [docs/guides/environnement-local.md](docs/guides/environnement-local.md). Cibles utiles : `make help`.

## Structure

```text
backend/            FastAPI, Celery, Alembic — Python 3.13, uv
  src/runtime/      composition root (settings, app, worker, santé)
  src/<contexte>/   interview_training, assessment (READMEs ; code à venir par vertical slices) ;
                    knowledge_base conçu (ADR 0007), créé à la slice S0
  src/shared/       primitives transverses justifiées uniquement
  tests/            unit, integration, architecture
frontend/           React 19, TypeScript strict, Vite, Vitest
docs/               context, conception, decisions (ADR), guides
compose.yaml        stack de développement ; compose.test.yaml : stack d'intégration éphémère
```

## Documentation

Règles de travail : [CLAUDE.md](CLAUDE.md) (Documentation First, respect des ADR, vertical slices, tests).


- [Index](docs/README.md)
- [Contexte métier](docs/context/domain.md)
- [Bounded Contexts](docs/conception/001-bounded-contexts.md)
- [Flux et événements](docs/conception/002-event-flows.md)
- [Agrégats et invariants](docs/conception/003-aggregates-and-invariants.md)
- [Contrats d'intégration](docs/conception/004-integration-contracts.md)
- [Structure Clean Architecture](docs/conception/005-project-structure.md)
- [Questions ouvertes](docs/conception/006-open-questions.md)
- [Knowledge Base](docs/conception/bounded-contexts/knowledge-base.md) — conception, [feuille de route S0–S9](docs/conception/bounded-contexts/knowledge-base-roadmap.md), [S0](docs/conception/bounded-contexts/knowledge-base-s0.md)
- [ADR](docs/decisions/README.md) — dont [ADR 0006 — Socle technique](docs/decisions/0006-technical-bootstrap.md) et [ADR 0007–0012 — Knowledge Base](docs/decisions/0007-knowledge-base-bounded-context.md)
- [Guide environnement local](docs/guides/environnement-local.md)

Architecture : monolithe modulaire DDD / Clean Architecture, FastAPI, PostgreSQL (Alembic), Qdrant (vecteurs), RabbitMQ/Celery, routage multi-LLM envisagé (Groq, Cerebras, Gemini, OpenRouter). Pipeline RAG de Knowledge Base : Docling, SentenceTransformers (`multilingual-e5-base` initial, révisable), Qdrant, sans framework RAG (ADR 0008).

Conventions inspirées de `salletti/trading-agent-ai` (`domain`, `use_cases`, `adapters`, `contracts`, `frameworks`, `runtime`).
