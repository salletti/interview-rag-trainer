# ADR 0006 — Socle technique de la Phase 1

- **Date :** 2026-11-17
- **Statut :** Proposed

## Contexte

Les ADR 0001 et 0002 fixent le découpage métier ; les ADR 0003 à 0005 et
`docs/conception/006-open-questions.md` listent des choix techniques encore
proposés. La Phase 1 doit fournir un environnement local démarrable en une
commande, sans implémenter de fonctionnalité métier. Les choix ci-dessous ont
été faits pour ce socle ; ils restent **proposés** jusqu'à validation.

## Décisions

1. **Monorepo `backend/` + `frontend/`.** Les Bounded Contexts restent sous
   `backend/src/<contexte>/` (ADR 0003) ; `runtime` est le composition root.
2. **Python 3.13, FastAPI, Pydantic v2, SQLAlchemy 2.x asyncio + asyncpg,
   Alembic, uv.** Versions résolues dans `backend/uv.lock`.
3. **PostgreSQL 18 via l'image `pgvector/pgvector`.** L'extension `vector`
   est *disponible* mais *non activée* : **Qdrant est le moteur vectoriel
   principal**. C'est une décision **nouvelle** de la mission Phase 1 : les
   documents antérieurs (`006-open-questions.md`, README initial)
   proposaient pgvector. Motif : un moteur dédié (filtrage par payload,
   collections versionnées, client asynchrone) découple le RAG du schéma
   relationnel des contextes et évite de mélanger tables métier et index
   vectoriels dans la même base. Un éventuel `CREATE EXTENSION vector`
   passera par une migration Alembic justifiée.
4. **Alembic est l'unique mécanisme de schéma.** `metadata.create_all()` est
   proscrit hors tests. Dans Compose, le service `migrate` exécute
   `upgrade head` avant l'API. Chaque contexte possédera son `MetaData`,
   combinés uniquement dans `alembic/env.py`.
5. **Qdrant v1.20 et client asynchrone**, initialisé dans `runtime`. Aucune
   collection en Phase 1.
6. **RabbitMQ 4.4 comme broker Celery ; result backend `rpc://`.** Redis
   n'est **pas** installé : aucun besoin de cache, de verrou distribué ni de
   result backend persistant n'existe. `rpc://` réutilise RabbitMQ et suffit à
   la tâche technique `runtime.ping`. Déclencheur de réintroduction : un
   besoin mesuré de cache ou de résultats consultables après redémarrage.
7. **Readiness.** `/health/ready` exige PostgreSQL (503 sinon) ; Qdrant et
   RabbitMQ sont sondés et rapportés mais non bloquants (`degraded`, 200).
   Motif : le RAG n'existe pas encore et la publication d'événements passera
   par une outbox PostgreSQL (ADR 0004), donc l'API peut servir sans eux.
   Chaque sonde est bornée par `EXTERNAL_TIMEOUT_SECONDS`.
8. **Les tâches Celery ne sont pas les Integration Events.** Celery est de
   l'outillage d'exécution ; les contrats inter-contextes restent des schémas
   JSON versionnés (004-integration-contracts.md) publiés via outbox, à venir.
9. **Frontend React 19 / TypeScript strict / Vite 8, ESLint typé, Prettier,
   Vitest + React Testing Library.** En développement, Vite relaie `/api` et
   `/health` vers l'API : **aucun CORS côté backend**. L'image de production
   sert les statiques par nginx avec le même relais.
10. **Journalisation standard** (`logging`) avec un format unique ; structlog
    n'est pas introduit sans besoin d'export structuré.
11. **Règles d'architecture testées** (`backend/tests/architecture/`) par
    analyse statique des imports : domaine sans framework, pas d'import des
    couches internes d'un autre contexte, aucun contexte ne dépend de
    `runtime`, `runtime` ne voit que la surface publique des contextes.
12. **Ports hôte décalés** (API 8010, frontend 5180, PostgreSQL 5440),
    configurables dans `.env`, pour cohabiter avec d'autres stacks locales.

## Alternatives écartées

- Redis comme result backend : installé « par habitude », aucun usage.
- pgvector comme moteur RAG V1 (proposition initiale de
  `006-open-questions.md`) : remplacé par Qdrant, voir décision 3.
- `psycopg` async : asyncpg retenu pour la maturité de son intégration
  SQLAlchemy asyncio ; le choix est confiné au DSN de `runtime.settings`.
- Readiness bloquante sur Qdrant/RabbitMQ : rejetée tant qu'aucun cas
  d'usage synchrone n'en dépend.

## Rapport à l'ADR 0004

L'ADR 0004 laisse « à trancher » la topologie RabbitMQ et Celery. Cet ADR ne
règle que le choix du broker et du result backend. Exchanges, files durables,
dead-letter, TTL des leases et politique de reconciliation restent ouverts et
seront traités avec l'outbox.

## Conséquences

- `docker compose up --build -d` démarre sept services (migrate, api, worker,
  frontend, postgres, qdrant, rabbitmq) ; `compose.test.yaml` fournit une
  stack éphémère pour les tests d'intégration.
- Les contextes métier n'ont toujours aucun code : les règles d'architecture
  s'appliqueront dès leur premier fichier.
- Ce document est à mettre à jour si une exclusion (Redis, pgvector actif) est
  levée, avec le besoin qui la motive.
