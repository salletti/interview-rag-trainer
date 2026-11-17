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
| Worker Celery (`worker`) | — | file `interview_rag_trainer.default` : `runtime.ping`, tâches `dispatch` et `recover` de Knowledge Base |
| Worker d'ingestion (`worker_ingestion`) | — | file `knowledge_base.ingestion`, `--concurrency=1 --prefetch-multiplier=1` (traitements CPU de Knowledge Base ; aucun en S0) |
| Scheduler (`scheduler`) | — | Celery beat : émet `dispatch` (2 s) et `recover` (30 s) ; **une seule instance** ; état dans `/tmp` |
| migrate | — | `alembic upgrade head`, s'arrête une fois terminé |

Les ports hôte sont décalés (8010, 5180, 5440) pour cohabiter avec d'autres projets ; ils se changent dans `.env`.

Ordre de démarrage garanti par Compose : PostgreSQL sain → `migrate` terminé → `api`, `worker` et `worker_ingestion` ; `scheduler` dès que RabbitMQ est sain ; `frontend` après `api`.

### Volumes

| Volume | Monté dans | Chemin | Usage |
|---|---|---|---|
| `postgres_data`, `qdrant_data`, `rabbitmq_data` | services d'infrastructure | — | données |
| `kb_blobs` | `api`, `worker`, `worker_ingestion` | `/var/lib/interview-rag-trainer/blobs` (`KB_BLOB_ROOT`) | originaux des documents de Knowledge Base (S2) : `sha256/<aa>/<bb>/<empreinte>`, temporaires d'upload sous `tmp/` |
| `models_cache` | `api`, `worker_ingestion` | `/var/cache/interview-rag-trainer/models` | cache des modèles (à partir de S3/S4) |

Les répertoires de montage sont créés dans l'image avec le propriétaire `app` : un volume nommé neuf en hérite les droits.

### Variables Knowledge Base (`KB_*`)

Défauts **Proposed** de [S0 §7 et §8.4](../conception/bounded-contexts/knowledge-base-s0.md#7-retry-et-backoff), transmis aux conteneurs par `compose.yaml` et documentés dans `.env.example`. Durées en secondes, valeurs strictement positives.

| Variable | Défaut | Rôle |
|---|---|---|
| `KB_WORK_BACKOFF_BASE_SECONDS` / `KB_WORK_BACKOFF_MAX_SECONDS` | 10 / 900 | backoff exponentiel plafonné, *equal jitter* |
| `KB_WORK_LEASE_TTL_SECONDS` | 300 | durée d'un lease |
| `KB_WORK_HEARTBEAT_INTERVAL_SECONDS` | lease / 3 | intervalle de heartbeat conseillé (strictement inférieur au lease) ; hors Compose seulement |
| `KB_WORK_TIME_LIMIT_SECONDS` | 3600 | limite dure Celery de la tâche `execute` |
| `KB_WORK_DISPATCH_INTERVAL_SECONDS` / `KB_WORK_RECOVER_INTERVAL_SECONDS` | 2 / 30 | planification de beat |
| `KB_WORK_DISPATCH_BATCH` | 50 | taille de lot de `dispatch` et de `recover` |
| `KB_WORK_REDISPATCH_AFTER_SECONDS` | 600 | délai avant une nouvelle notification d'un item `PENDING` |
| `KB_WORK_MAX_IN_FLIGHT` | 2 | plafond d'items en vol **par file** (en S0 : `knowledge_base.ingestion`) |
| `KB_MAX_UPLOAD_BYTES` | 20971520 (20 Mio) | taille maximale d'un original importé (ADR 0012) ; au-delà, 413 sans blob ni ligne |
| `KB_BLOB_ROOT` | `/var/lib/interview-rag-trainer/blobs` | racine du volume des blobs ; fixée par `compose.yaml` au point de montage de `kb_blobs` (non surchargeable par `.env`) ; hors Docker, à pointer vers un répertoire inscriptible |

## Vérifications

```bash
make ps                                  # état et healthchecks
curl -s localhost:8010/health/ready      # {"status":"ready", "checks": {...}}
make worker-ping                         # pong : broker → worker → result backend
docker compose exec worker_ingestion celery -A runtime.worker:celery_app inspect active_queues
                                         # knowledge_base.ingestion : consommée par worker_ingestion seul
```

`/health/ready` répond `ready` (200), `degraded` (200, Qdrant ou RabbitMQ indisponible) ou `not_ready` (503, PostgreSQL indisponible). Voir ADR 0006.

### Importer un document (Knowledge Base, S2)

```bash
curl -s -w '\nHTTP %{http_code}\n' -X POST localhost:8010/api/v1/knowledge-base/documents \
  -F 'file=@cours.md;type=text/markdown' \
  -F title='Asyncio' -F kind=COURSE -F language=fr -F topics=python -F topics=asyncio \
  -H 'Idempotency-Key: import-asyncio-1'          # facultatif
# 202 {"document_id": "...", "version_id": "...", "number": 1}
# même fichier une seconde fois : 200, même corps

curl -s -X POST localhost:8010/api/v1/knowledge-base/documents/<document_id>/versions \
  -F 'file=@cours-v2.md;type=text/markdown'      # 202, number = 2
```

`;type=text/markdown` est nécessaire : sans lui, curl envoie `application/octet-stream` pour un `.md` et l'API répond 415 (`UNSUPPORTED_MEDIA_TYPE`). Types acceptés : `text/markdown` (`.md`, `.markdown`) et `text/plain` (`.txt`). Codes d'erreur et contrat complet : [README de Knowledge Base](../../backend/src/knowledge_base/README.md). Vérifications :

```bash
docker compose exec postgres psql -U interview_rag_trainer -d interview_rag_trainer \
  -c "select number, status, content_hash from knowledge_base.document_versions" \
  -c "select kind, status from knowledge_base.work_items where kind = 'extract_version'"
docker compose exec api find /var/lib/interview-rag-trainer/blobs -type f
```

Jusqu'à S3, l'item `extract_version` reste `PENDING` : aucun handler n'est enregistré pour ce `kind`, le dispatcher l'ignore.

## Qualité et tests

```bash
make lint typecheck test                 # Ruff, mypy strict, pytest unit + architecture
make frontend-lint frontend-test         # ESLint, Prettier, tsc, Vitest
make ci                                  # tout ce qui précède
make test-integration                    # stack compose.test.yaml éphémère + pytest -m integration
```

Les tests d'intégration (re)construisent l'image, démarrent PostgreSQL (5441), Qdrant (6343), RabbitMQ (5682) et un worker, appliquent `alembic upgrade head`, puis détruisent la stack. `rabbitmq_test` monte `backend/tests/integration/rabbitmq_test/20-consumer-timeout.conf` (`consumer_timeout` de 5 s, scénario 14 de S0 uniquement) et le Makefile exporte `KB_TEST_CONSUMER_TIMEOUT_SECONDS=5` ; sans cette variable, le scénario 14 est ignoré explicitement. Ce scénario dure environ 1 min 30 ; les scénarios 13 et 14 lancent un vrai worker prefork en sous-processus.

## Migrations

```bash
make migration m="slug_de_la_migration"  # crée un fichier sous backend/alembic/versions/ (volume monté)
make migrate                             # applique dans la stack de dev
```

Jamais de `metadata.create_all()` : Alembic est l'unique mécanisme de schéma. `backend/alembic/env.py` déclare le `MetaData` de chaque contexte (Knowledge Base : schéma `knowledge_base`) avec `include_schemas=True` ; seuls ces schémas sont comparés, la table de versions reste dans `public`. `cd backend && uv run alembic check` vérifie qu'aucune migration ne manque.

## Développement hors Docker

```bash
cd backend && uv sync && uv run uvicorn runtime.app:create_app --factory --reload --port 8010
cd frontend && npm ci && npm run dev     # proxy vers http://localhost:8010 par défaut
```

Le backend lit `../.env` (racine du dépôt) puis `.env` (répertoire courant), et les variables d'environnement l'emportent. Sans `.env`, ses défauts visent la stack de développement (PostgreSQL 5440, RabbitMQ 5672 avec l'utilisateur `interview_rag_trainer`).

## Arrêt

```bash
make down                 # conserve les volumes (données PostgreSQL, Qdrant, RabbitMQ, kb_blobs, models_cache)
docker compose down -v    # supprime aussi les volumes
```
