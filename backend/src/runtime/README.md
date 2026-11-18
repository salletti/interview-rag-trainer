# Runtime

Composition root technique du monolithe. Aucune règle métier.

| Module | Rôle |
|---|---|
| `settings.py` | Pydantic Settings : hôtes, ports, secrets (`SecretStr`), délais |
| `app.py` | `create_app()` : FastAPI, `lifespan` (moteur SQLAlchemy, client Qdrant), routeurs ; `knowledge_base.register(app, ressources, settings)` monte les routes d'import et de recherche, `KnowledgeBaseResources` reçoit la fabrique de sessions, les réglages `KB_*` et le client Qdrant du processus |
| `database.py` | `AsyncEngine`, fabrique de sessions, dépendance « une `AsyncSession` par requête », sonde |
| `vector_store.py` | client `AsyncQdrantClient` et sonde |
| `messaging.py` | sonde RabbitMQ (Kombu dans un thread, bornée) |
| `worker.py` | application Celery de production (`celery -A runtime.worker:celery_app worker` / `beat`), tâche technique `runtime.ping` |
| `worker_factory.py` | `create_celery_app(settings=None, *, work_handlers=None)` : configuration Celery (dont `worker_prefetch_multiplier=1`), `knowledge_base.register_worker`, ressources des processus enfants (signaux) ; `work_handlers=None` = handlers de production (`knowledge_base.build_work_handlers`), une séquence les remplace (tests) |
| `provision.py` | `python -m runtime.provision` : provisionne l'index initial de Knowledge Base (`knowledge_base.provision_search_index`), idempotent ; service Compose `kb_provision`, avant l'API et les workers ; code de sortie non nul si PostgreSQL ou Qdrant manquent |
| `health.py` | `/health/live`, `/health/ready` (PostgreSQL requis, Qdrant/RabbitMQ informatifs) ; ni l'un ni l'autre ne charge le modèle d'embeddings |
| `system.py` | `/api/v1/system/info` (données non sensibles) |
| `logging_config.py` | logging standard, format unique |

## Workers Celery et Knowledge Base

Détail : [knowledge-base-s0.md §9](../../../docs/conception/bounded-contexts/knowledge-base-s0.md#9-ressources-asyncio-dans-un-worker-celery-prefork).

| Moment | Processus | Ce que fait `runtime.worker_factory` |
|---|---|---|
| construction de l'application (import de `runtime.worker`) | maître | `knowledge_base.register_worker(app, fournisseur)` : tâches, routes (`dispatch` et `recover` sur `interview_rag_trainer.default`), `beat_schedule`. Aucune connexion, aucune boucle. |
| `worker_process_init` | chaque enfant prefork (et le processus d'un pool solo) | une boucle `asyncio.new_event_loop()` persistante, puis le moteur asyncpg (`pool_size=2`), le client Qdrant, les handlers de production construits sur la fabrique de sessions et ce client (`knowledge_base.build_work_handlers` : `extract_version`, `index_version`, `sync_visibility` ; le modèle d'embeddings n'est chargé qu'à la première tâche) et `KnowledgeBaseWorkerResources` |
| exécution d'une tâche | enfant | `loop.run_until_complete(...)` sur cette boucle ; jamais `asyncio.run()` par tâche |
| `worker_process_shutdown` | enfant | fermeture du client Qdrant, `engine.dispose()`, fermeture de la boucle |

- Un processus ne construit qu'**une** application qui enregistre les tâches de Knowledge Base. Sous macOS, billiard démarre les enfants en *spawn* : Celery y partage chaque tâche entre toutes les applications du processus (la première enregistrée sous un nom l'emporte) et ne reconstruit les tâches qu'en important les modules de `conf.include`, d'où `celery_app.conf.include = ["runtime.worker"]`. Sous Linux (conteneurs), les enfants sont forkés.
- Processus : `worker` (file par défaut, `--concurrency=2`), `worker_ingestion` (`-Q knowledge_base.ingestion --concurrency=1 --prefetch-multiplier=1`), `scheduler` (`celery beat`, une seule instance, n'accède pas à la base). L'API n'enregistre rien pour Knowledge Base en S0.

Les Bounded Contexts sont enregistrés ici par leur surface publique uniquement (`backend/tests/architecture/`) : points d'entrée réexportés par leur package racine (`register`, `register_worker`, fabriques de contrats), voir [005 — Surface publique](../../../docs/conception/005-project-structure.md#surface-publique-dun-contexte-et-composition-root).

## Recherche de Knowledge Base dans l'API (S5)

- Le modèle d'embeddings (`intfloat/multilingual-e5-base`) est chargé par l'API à la **première recherche**, dans un thread, puis gardé par le processus : le démarrage et `/health/ready` n'en dépendent pas. Mesuré le 2026-11-18 sur la stack de dev : premier appel 4,3 s, RSS du processus serveur 146 → 1 267 Mio. Volume `models_cache` monté dans `api` ; `HF_HUB_DISABLE_XET=1` vient de l'environnement commun de `compose.yaml`.
- Pour fournir le contrat `KnowledgeSearch` à un autre contexte, `runtime` appellera `knowledge_base.build_knowledge_search(session_factory, qdrant_client, settings)` une fois, à la composition, et passera la même instance au consommateur ([005](../../../docs/conception/005-project-structure.md#transmission-dun-contrat-public-à-un-autre-contexte)) ; aucun consommateur n'existe en S5.
