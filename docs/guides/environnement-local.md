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
| Worker d'ingestion (`worker_ingestion`) | — | file `knowledge_base.ingestion`, `--concurrency=1 --prefetch-multiplier=1` : `extract_version`, `index_version`, `sync_visibility` ; charge le modèle d'embeddings à sa première tâche (~1,7 Gio de RAM ensuite) et Docling PDF à son premier PDF (pic de RSS mesuré : 2,6 Gio pour un PDF de 300 pages, modèle d'embeddings compris) |
| Scheduler (`scheduler`) | — | Celery beat : émet `dispatch` (2 s) et `recover` (30 s) ; **une seule instance** ; état dans `/tmp` |
| migrate | — | `alembic upgrade head`, s'arrête une fois terminé |
| kb_provision | — | `python -m runtime.provision` : crée la collection Qdrant et active l'index initial de Knowledge Base s'il n'existe pas (idempotent), s'arrête une fois terminé |

Les ports hôte sont décalés (8010, 5180, 5440) pour cohabiter avec d'autres projets ; ils se changent dans `.env`.

Ordre de démarrage garanti par Compose : PostgreSQL sain → `migrate` terminé → `kb_provision` terminé (Qdrant sain) → `api`, `worker` et `worker_ingestion` ; `scheduler` dès que RabbitMQ est sain ; `frontend` après `api`.

### Volumes

| Volume | Monté dans | Chemin | Usage |
|---|---|---|---|
| `postgres_data`, `qdrant_data`, `rabbitmq_data` | services d'infrastructure | — | données |
| `kb_blobs` | `api`, `worker`, `worker_ingestion` | `/var/lib/interview-rag-trainer/blobs` (`KB_BLOB_ROOT`) | originaux des documents de Knowledge Base (S2) : `sha256/<aa>/<bb>/<empreinte>`, temporaires d'upload sous `tmp/` |
| `models_cache` | `api`, `worker_ingestion` | `/var/cache/interview-rag-trainer/models` (`KB_MODELS_CACHE_DIR`) | poids du modèle d'embeddings (S4, ~1,1 Go), téléchargés une fois puis relus ; depuis S6, poids de Docling PDF (layout et TableFormer, ~506 Mio) sous `huggingface/` (`HF_HOME`), téléchargés par `worker_ingestion` au premier PDF |

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
| `KB_MODELS_CACHE_DIR` | `/var/cache/interview-rag-trainer/models` | cache des poids du modèle (S4) ; fixé par `compose.yaml` au point de montage de `models_cache` ; hors Docker, à pointer vers un répertoire inscriptible (ex. `backend/.cache/models`) |
| `KB_EMBEDDING_BATCH_SIZE` | 32 | chunks encodés par appel au modèle, donc par upsert Qdrant |
| `HF_HOME` (non `KB_`) | `/var/cache/interview-rag-trainer/models/huggingface` | cache du Hub pour les poids de Docling PDF (S6) ; fixé par `compose.yaml` dans le volume `models_cache` (non surchargeable par `.env`) |
| `HF_HUB_DISABLE_XET` (non `KB_`) | 1 dans Compose | premier téléchargement en HTTPS simple : le protocole *xet* du Hub est resté bloqué à 0 octet dans notre environnement (2026-11-18) ; `0` pour le réactiver |

### Premier démarrage : le modèle d'embeddings

Le modèle `intfloat/multilingual-e5-base` (révision épinglée dans le profil) n'est pas dans l'image : `worker_ingestion` le télécharge dans le volume `models_cache` à sa **première tâche** (l'extraction compte les tokens avec son tokenizer), puis le relit à chaque démarrage (~4 s). Le premier import attend donc ce téléchargement (1,1 Go) ; pendant ce temps, le worker prolonge le lease de l'item. Suivi : `docker compose logs -f worker_ingestion` (message « modèle d'embeddings … chargé en … s »). `docker compose down -v` supprime aussi ce cache.

## Vérifications

```bash
make ps                                  # état et healthchecks
curl -s localhost:8010/health/ready      # {"status":"ready", "checks": {...}}
make worker-ping                         # pong : broker → worker → result backend
docker compose run --rm kb_provision     # relance le provisionnement : sans effet si l'index existe
docker compose exec worker_ingestion celery -A runtime.worker:celery_app inspect active_queues
                                         # knowledge_base.ingestion : consommée par worker_ingestion seul
```

`/health/ready` répond `ready` (200), `degraded` (200, Qdrant ou RabbitMQ indisponible) ou `not_ready` (503, PostgreSQL indisponible). Voir ADR 0006. Qdrant reste non bloquant depuis S5 : sans lui, l'import de documents fonctionne, seule la recherche échoue (500).

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

`;type=text/markdown` est nécessaire : sans lui, curl envoie `application/octet-stream` pour un `.md` et l'API répond 415 (`UNSUPPORTED_MEDIA_TYPE`). Types acceptés : `text/markdown` (`.md`, `.markdown`), `text/plain` (`.txt`) et, depuis S6, `application/pdf` (`.pdf`) et `application/vnd.openxmlformats-officedocument.wordprocessingml.document` (`.docx`) ; le type doit concorder avec l'extension, le Word binaire (`.doc`) est refusé. Codes d'erreur et contrat complet : [README de Knowledge Base](../../backend/src/knowledge_base/README.md). Vérifications :

```bash
docker compose exec postgres psql -U interview_rag_trainer -d interview_rag_trainer \
  -c "select number, status, content_hash from knowledge_base.document_versions" \
  -c "select kind, status from knowledge_base.work_items where kind = 'extract_version'"
docker compose exec api find /var/lib/interview-rag-trainer/blobs -type f
```

Depuis S4, l'import va jusqu'à la publication : `extract_version` (version `EXTRACTED`, une `Indexation PENDING` et un item `index_version` par index actif), `index_version` (embeddings, points Qdrant `visible=false`, manifest vérifié, `Indexation COMPLETED`, version `PUBLISHED`, item `sync_visibility`), puis `sync_visibility` (points `visible=true`) :

```bash
docker compose exec postgres psql -U interview_rag_trainer -d interview_rag_trainer \
  -c "select status from knowledge_base.document_versions" \
  -c "select status, point_count, manifest_digest from knowledge_base.indexations" \
  -c "select kind, status, attempts from knowledge_base.work_items order by created_at"
curl -s localhost:6333/collections                         # kb_index_<index_id>
curl -s -X POST localhost:6333/collections/kb_index_<index_id>/points/count \
  -H 'Content-Type: application/json' \
  -d '{"exact": true, "filter": {"must": [{"key": "visible", "match": {"value": true}}]}}'
```

### Importer un PDF ou un DOCX (Knowledge Base, S6)

```bash
curl -s -X POST localhost:8010/api/v1/knowledge-base/documents \
  -F 'file=@backend/tests/fixtures/knowledge_base/office/cours_concurrence_long_fr.pdf;type=application/pdf' \
  -F title='Concurrence en Python' -F kind=COURSE -F language=fr -F topics=python
curl -s -X POST localhost:8010/api/v1/knowledge-base/documents \
  -F 'file=@backend/tests/fixtures/knowledge_base/office/cours_git_fr.docx;type=application/vnd.openxmlformats-officedocument.wordprocessingml.document' \
  -F title='Cours Git' -F kind=COURSE -F language=fr -F topics=git
```

Le **premier PDF** télécharge les poids de Docling (layout `docling-layout-heron`, TableFormer : ~506 Mio) dans `models_cache/huggingface` : compter environ trois minutes de plus, pendant lesquelles le worker prolonge le lease ; suivi par `docker compose logs -f worker_ingestion` (« Downloaded model … », puis « … page(s) converties par Docling en … s »). Les suivants relisent le cache. Ordres de grandeur dans `worker_ingestion` (Linux arm64, 18 CPU) : de 1 à 3 s par page à chaud (2,0 s/page pour un PDF de 300 pages, 15 min de l'import à `PUBLISHED` en comptant l'encodage de ses 682 chunks) ; un DOCX ne charge aucun modèle (quelques millisecondes par page équivalente). Échecs terminaux visibles dans `document_versions.failure_code` : `OCR_REQUIRED` (PDF scanné, sans couche texte), `TOO_MANY_PAGES` (plus de 300 pages), `INVALID_DOCUMENT` (chiffré, corrompu, DOCX illisible), `DOCUMENT_TOO_COMPLEX` (DOCX de plus de 100 Mio de XML décompressé). Les passages issus d'un PDF portent `page_start` / `page_end` dans leur citation ; ceux d'un DOCX, `null`.

**Test de fumée du parseur PDF dans l'image** (après une modification du `Dockerfile` ou de Docling) :

```bash
make smoke-pdf-image
# cours_git_fr.pdf : 14 blocs en 7.9 s (CodeBlock 1, Heading 7, Paragraph 6)
# OK : titres et blocs de code reconnus
```

La cible lance `backend/scripts/smoke_pdf_parser.py` dans un conteneur éphémère de l'image de `worker_ingestion` (`docker compose run --rm --no-deps` : volumes du service, dont `models_cache`, sans toucher au worker en cours ; script et fixture montés depuis l'hôte). Elle échoue (code 1) si le PDF ne donne pas au moins un titre et un bloc de code : c'est ce qui arrive quand les polices de rendu des pages manquent à l'image (rectangles à la place du texte pour le modèle de layout), écart que les tests d'intégration, exécutés sur l'hôte, ne voient pas. Les poids de Docling doivent être dans `models_cache` (sinon premier téléchargement, ~3 min). Image à jour : `docker compose build worker_ingestion`.

### Rechercher (Knowledge Base, S5)

Une fois la version `PUBLISHED` et `sync_visibility` passé (quelques secondes après l'import) :

```bash
curl -s -X POST localhost:8010/api/v1/knowledge-base/search \
  -H 'Content-Type: application/json' \
  -d '{"text": "Comment rejouer un test avec plusieurs jeux de valeurs ?", "limit": 3, "language": "fr", "topics": ["pytest"]}'
# 200 {"index_id": "...", "complete": true, "passages": [{"unit_id": "...", "unit_type": "QA",
#      "question": "...", "text": "Question : ...\n\n...", "truncated": false, "matched_chunk_ids": ["..."],
#      "score": 0.87, "citation": {"document_id": "...", "document_title": "...", "version_id": "...",
#      "version_number": 1, "section_path": ["..."], "page_start": null, "page_end": null,
#      "source_filename": "qa_pytest_fr.md"}}]}
```

Seul `text` est obligatoire ; `limit` de 1 à 50 (5 par défaut) ; filtres facultatifs `language`, `topics` (tous requis), `kinds` et `document_ids` (l'un d'eux). 422 si le corps est invalide, 503 `NO_ACTIVE_SEARCH_INDEX` si `kb_provision` n'a pas tourné. La **première** recherche charge le modèle dans l'API (~4 s, RSS du processus serveur ~1,3 Gio ensuite) ; les suivantes prennent ~70 ms (médiane mesurée en local, HTTP compris). Variables : `KB_SEARCH_OVERSAMPLING_FACTOR` (2), `KB_SEARCH_COMPLEMENT_PASSES` (1), `KB_MAX_PASSAGE_TOKENS` (1024), voir `.env.example`.

### Retirer un document (Knowledge Base, S7)

```bash
curl -s -X DELETE localhost:8010/api/v1/knowledge-base/documents/<document_id>
# 202 {"document_id": "...", "status": "WITHDRAWN", "visibility_revision": 2}
```

Le retrait est immédiat : toute recherche lancée après la réponse ignore le document (contrôle SQL), même si Qdrant porte encore ses points. Le worker d'ingestion exécute ensuite l'item `purge_document` (quelques secondes, intervalle du dispatch) : points Qdrant du document, puis unités et chunks avec `knowledge_base.documents.purged_at` (une transaction), puis originaux du volume `kb_blobs` (si leur suppression échoue, le worker journalise un avertissement et le nettoyage des orphelins les reprendra). Le document et ses versions restent en base (tombstones) : un second `DELETE` répond 409 `DOCUMENT_WITHDRAWN`, une nouvelle version ou un réimport du même contenu aussi ; un identifiant inconnu donne 404 `DOCUMENT_NOT_FOUND`.

Un client qui **rejoue** un `DELETE` (délai dépassé, réponse perdue, nouvelle tentative automatique) doit traiter 409 `DOCUMENT_WITHDRAWN` comme un **succès** : le premier appel a abouti, le document est retiré et sa purge planifiée ; le rejeu, lui, ne change rien. Seul 409 `CONCURRENT_MODIFICATION` appelle une nouvelle tentative. Vérification :

```bash
docker compose exec postgres psql -U interview_rag_trainer -d interview_rag_trainer -c \
  "SELECT status, purged_at FROM knowledge_base.documents WHERE id = '<document_id>'"
docker compose exec postgres psql -U interview_rag_trainer -d interview_rag_trainer -c \
  "SELECT kind, status FROM knowledge_base.work_items WHERE dedup_key = 'purge_document:<document_id>'"
```

Le nettoyage des blobs orphelins (fichiers sans document non purgé, plus vieux que `KB_BLOB_ORPHAN_GRACE_SECONDS`, 24 h par défaut) existe comme cas d'usage (`build_clean_blob_orphans_handler`) ; son déclenchement périodique arrive avec la réconciliation (S8).

## Qualité et tests

```bash
make lint typecheck test                 # Ruff, mypy strict, pytest unit + architecture
make frontend-lint frontend-test         # ESLint, Prettier, tsc, Vitest
make ci                                  # tout ce qui précède
make test-integration                    # stack compose.test.yaml éphémère + pytest -m integration
```

L'évaluation de la recherche (marqueur `retrieval`, inclus dans `make test-integration`) écrit sa mesure dans `backend/.cache/retrieval/retrieval_report.json` (ou `KB_RETRIEVAL_REPORT_DIR`) et la compare aux seuils de `backend/tests/fixtures/knowledge_base/retrieval/retrieval_thresholds.json`.

Les tests d'intégration incluent les tests marqués `model` (vrai `multilingual-e5-base` ; depuis S6, vrais modèles de layout et TableFormer de Docling) : le Makefile pointe `SENTENCE_TRANSFORMERS_HOME` vers `backend/.cache/models` et `HF_HOME` vers `backend/.cache/huggingface` (ignorés par git), remplis au premier lancement (1,1 Go et ~506 Mio ; `HF_HUB_DISABLE_XET=1`). Les mesures d'extraction PDF du test `test_office_parsers_live` sont écrites dans `backend/.cache/extraction/office_extraction_report.json` (ou `KB_EXTRACTION_REPORT_DIR`), celles de l'évaluation S6 dans `retrieval_report_office.json` et `retrieval_report_long_units.json`, à côté du rapport de S5. Pour les exclure : `cd backend && uv run pytest -m "integration and not model"` avec l'environnement de `INTEGRATION_ENV`. Les tests d'intégration (re)construisent l'image, démarrent PostgreSQL (5441), Qdrant (6343), RabbitMQ (5682) et un worker, appliquent `alembic upgrade head`, puis détruisent la stack. `rabbitmq_test` monte `backend/tests/integration/rabbitmq_test/20-consumer-timeout.conf` (`consumer_timeout` de 5 s, scénario 14 de S0 uniquement) et le Makefile exporte `KB_TEST_CONSUMER_TIMEOUT_SECONDS=5` ; sans cette variable, le scénario 14 est ignoré explicitement. Ce scénario dure environ 1 min 30 ; les scénarios 13 et 14 lancent un vrai worker prefork en sous-processus.

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
