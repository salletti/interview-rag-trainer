# Guide — Environnement de développement local

## Prérequis

- Docker Desktop (Compose v2).
- Hors Docker, pour les outils de qualité : `uv` (backend) et Node ≥ 22.12 (frontend). Aucune installation globale n'est nécessaire : `uv` crée `backend/.venv`, `npm` installe dans `frontend/node_modules`.

## Démarrage

```bash
cp .env.example .env        # facultatif : les valeurs par défaut suffisent en local
docker compose up --build -d
```

| Service | URL / port hôte (défaut) | Rôle |
|---|---|---|
| API FastAPI | http://localhost:8010 — `/docs`, `/health/live`, `/health/ready`, `/api/v1/system/info` | backend |
| Frontend Vite | http://localhost:5180 | React, proxy `/api` et `/health` vers l'API |
| PostgreSQL 18 | `localhost:5440` | image pgvector, extension non activée |
| Qdrant | http://localhost:6333 (REST), 6334 (gRPC) | moteur vectoriel principal |
| RabbitMQ | `localhost:5672`, management http://localhost:15672 | broker Celery |
| Worker Celery | — | file `interview_rag_trainer.default` |
| migrate | — | `alembic upgrade head`, s'arrête une fois terminé |

Les ports hôte sont décalés (8010, 5180, 5440) pour cohabiter avec d'autres projets ; ils se changent dans `.env`.

Ordre de démarrage garanti par Compose : PostgreSQL sain → `migrate` terminé → `api` et `worker` ; `frontend` après `api`.

## Vérifications

```bash
make ps                                  # état et healthchecks
curl -s localhost:8010/health/ready      # {"status":"ready", "checks": {...}}
make worker-ping                         # pong : broker → worker → result backend
```

`/health/ready` répond `ready` (200), `degraded` (200, Qdrant ou RabbitMQ indisponible) ou `not_ready` (503, PostgreSQL indisponible). Voir ADR 0006.

## Qualité et tests

```bash
make lint typecheck test                 # Ruff, mypy strict, pytest unit + architecture
make frontend-lint frontend-test         # ESLint, Prettier, tsc, Vitest
make ci                                  # tout ce qui précède
make test-integration                    # stack compose.test.yaml éphémère + pytest -m integration
```

Les tests d'intégration démarrent PostgreSQL (5441), Qdrant (6343), RabbitMQ (5682) et un worker, appliquent `alembic upgrade head`, puis détruisent la stack.

## Migrations

```bash
make migration m="slug_de_la_migration"  # crée un fichier sous backend/alembic/versions/ (volume monté)
make migrate                             # applique dans la stack de dev
```

Jamais de `metadata.create_all()` : Alembic est l'unique mécanisme de schéma. L'`--autogenerate` fonctionnera dès qu'un Bounded Context aura enregistré son `MetaData` dans `backend/alembic/env.py`.

## Développement hors Docker

```bash
cd backend && uv sync && uv run uvicorn runtime.app:create_app --factory --reload --port 8010
cd frontend && npm ci && npm run dev     # proxy vers http://localhost:8010 par défaut
```

Le backend lit `../.env` (racine du dépôt) puis `.env` (répertoire courant), et les variables d'environnement l'emportent. Sans `.env`, ses défauts visent la stack de développement (PostgreSQL 5440, RabbitMQ 5672 avec l'utilisateur `interview_rag_trainer`).

## Arrêt

```bash
make down                 # conserve les volumes (données PostgreSQL, Qdrant, RabbitMQ)
docker compose down -v    # supprime aussi les volumes
```
