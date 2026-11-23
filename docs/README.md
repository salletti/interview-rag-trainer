# Documentation — Index

Règles de travail permanentes (Documentation First, ADR, frontières, tests) : [CLAUDE.md](../CLAUDE.md).

## Référentiel de conception

- [Contexte métier](context/domain.md) — objectif produit, cas d'usage, langage ubiquitaire.
- [001 — Bounded Contexts](conception/001-bounded-contexts.md) — cartographie et ownership.
- [002 — Flux événementiels](conception/002-event-flows.md) — commandes, handlers, transactions, retries.
- [003 — Agrégats](conception/003-aggregates-and-invariants.md) — états et invariants.
- [004 — Contrats](conception/004-integration-contracts.md) — integration events versionnés.
- [005 — Structure](conception/005-project-structure.md) — Clean Architecture inspirée de trading-agent-ai, structure interne de chaque contexte, surface publique et composition root.
- [006 — Questions ouvertes](conception/006-open-questions.md) — décisions restant à prendre.
- [ADR](decisions/README.md) — décisions validées ou proposées.

## Knowledge Base

- [Conception du Bounded Context](conception/bounded-contexts/knowledge-base.md) — langage, agrégats, états, work items, visibilité, recherche, contrat `KnowledgeSearch`, points ouverts.
- [Feuille de route S0–S9](conception/bounded-contexts/knowledge-base-roadmap.md) — périmètre, dépendances, tests et critères par slice.
- [S0 — Infrastructure de traitement durable](conception/bounded-contexts/knowledge-base-s0.md) — schéma `work_items`, transitions, fencing, scheduler, asyncio dans Celery.
- [Benchmark des embeddings](conception/bounded-contexts/knowledge-base-embedding-benchmark.md) — 4 profils mesurés (S9), base de l'ADR 0013.
- [Suivi de l'objectif S0–S9](conception/bounded-contexts/knowledge-base-progress.md) · [validation globale](conception/bounded-contexts/knowledge-base-validation.md) · [dossier](conception/bounded-contexts/knowledge-base-independent-review.md) et [rapport](conception/bounded-contexts/knowledge-base-independent-review-report.md) de review indépendante.
- ADR : [0007](decisions/0007-knowledge-base-bounded-context.md) contexte · [0008](decisions/0008-custom-pipeline-docling-extraction.md) pipeline et Docling · [0009](decisions/0009-pedagogical-chunking-knowledge-units.md) chunking · [0010](decisions/0010-postgresql-source-of-truth-qdrant-derived-index.md) PostgreSQL / Qdrant · [0011](decisions/0011-embedding-profile-search-index-blue-green.md) profils et blue/green · [0012](decisions/0012-original-document-storage-and-purge.md) stockage · [0013](decisions/0013-definitive-embedding-profile.md) profil définitif (Proposed) · [0004](decisions/0004-reliable-messaging.md#complément--work-queue-durable-accepted-2026-11-17) work queue.

## Guides

- [Environnement de développement local](guides/environnement-local.md) — démarrage Docker Compose, ports, tests, migrations.

Les décisions explicitement validées sont **Accepted** ; les hypothèses techniques restent **Proposed**. Chaque décision structurante doit être historisée.
