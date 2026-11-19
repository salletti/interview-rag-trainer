# Runtime

Composition root technique du monolithe. Aucune règle métier.

| Module | Rôle |
|---|---|
| `settings.py` | Pydantic Settings : hôtes, ports, secrets (`SecretStr`), délais |
| `app.py` | `create_app()` : FastAPI, `lifespan` (moteur SQLAlchemy, client Qdrant), routeurs ; `knowledge_base.register(app, ressources)` monte les routes d'import et de recherche, `KnowledgeBaseResources` reçoit la fabrique de sessions, les réglages `KB_*` (seule source de réglages de Knowledge Base dans l'API) et le client Qdrant du processus, et porte les modèles d'embeddings paresseux de la recherche (un par profil du catalogue des cibles, S9) |
| `database.py` | `AsyncEngine`, fabrique de sessions, dépendance « une `AsyncSession` par requête », sonde |
| `vector_store.py` | client `AsyncQdrantClient` et sonde |
| `messaging.py` | sonde RabbitMQ (Kombu dans un thread, bornée) |
| `worker.py` | application Celery de production (`celery -A runtime.worker:celery_app worker` / `beat`), tâche technique `runtime.ping` |
| `worker_factory.py` | `create_celery_app(settings=None, *, work_handlers=None)` : configuration Celery (dont `worker_prefetch_multiplier=1`), `knowledge_base.register_worker`, ressources des processus enfants (signaux) ; `work_handlers=None` = handlers de production (`knowledge_base.build_work_handlers`), une séquence les remplace (tests) |
| `provision.py` | `python -m runtime.provision` : provisionne l'index initial de Knowledge Base (`knowledge_base.provision_search_index`), idempotent ; service Compose `kb_provision`, avant l'API et les workers ; code de sortie non nul si PostgreSQL ou Qdrant manquent |
| `index_migration.py` | `python -m runtime.index_migration start <cible>` / `activate <index_id>` (S9) : migration blue/green de l'index de recherche par la surface publique (`knowledge_base.start_index_migration`, `activate_search_index`, `migration_target_names`) ; `start` affiche l'index `BUILDING` (rejouable), `activate` bascule ou sort en erreur avec le refus (couverture incomplète, synchronisation en attente) ; cibles `make kb-migration-start target=…` et `make kb-migration-activate index=…`. Pas de route HTTP (décision S9) |
| `health.py` | `/health/live`, `/health/ready` (PostgreSQL requis, Qdrant/RabbitMQ informatifs : sans Qdrant, l'API sert encore l'import, seule la recherche échoue, en 500) ; ni l'un ni l'autre ne charge le modèle d'embeddings |
| `system.py` | `/api/v1/system/info` (données non sensibles) |
| `logging_config.py` | logging standard, format unique |

## Workers Celery et Knowledge Base

Détail : [knowledge-base-s0.md §9](../../../docs/conception/bounded-contexts/knowledge-base-s0.md#9-ressources-asyncio-dans-un-worker-celery-prefork).

| Moment | Processus | Ce que fait `runtime.worker_factory` |
|---|---|---|
| construction de l'application (import de `runtime.worker`) | maître | `knowledge_base.register_worker(app, fournisseur)` : tâches, routes (`dispatch`, `recover` et, depuis S8, `reconcile` sur `interview_rag_trainer.default`), `beat_schedule`. Aucune connexion, aucune boucle. |
| `worker_process_init` | chaque enfant prefork (et le processus d'un pool solo) | une boucle `asyncio.new_event_loop()` persistante, puis le moteur asyncpg (`pool_size=2`), le client Qdrant, les handlers de production construits sur la fabrique de sessions et ce client (`knowledge_base.build_work_handlers` : `extract_version`, `index_version`, `sync_visibility`, `purge_document`, `reconcile_knowledge_base`, et depuis S9 `rechunk_version`, `drop_index_collection` ; chaque modèle d'embeddings n'est chargé qu'à la première tâche qui en a besoin) et `KnowledgeBaseWorkerResources` |
| exécution d'une tâche | enfant | `loop.run_until_complete(...)` sur cette boucle ; jamais `asyncio.run()` par tâche |
| `worker_process_shutdown` | enfant | fermeture du client Qdrant, `engine.dispose()`, fermeture de la boucle |

- Un processus ne construit qu'**une** application qui enregistre les tâches de Knowledge Base. Sous macOS, billiard démarre les enfants en *spawn* : Celery y partage chaque tâche entre toutes les applications du processus (la première enregistrée sous un nom l'emporte) et ne reconstruit les tâches qu'en important les modules de `conf.include`, d'où `celery_app.conf.include = ["runtime.worker"]`. Sous Linux (conteneurs), les enfants sont forkés.
- Processus : `worker` (file par défaut, `--concurrency=2`), `worker_ingestion` (`-Q knowledge_base.ingestion --concurrency=1 --prefetch-multiplier=1 --max-tasks-per-child=${KB_INGESTION_MAX_TASKS_PER_CHILD:-50}`), `scheduler` (`celery beat`, une seule instance, n'accède pas à la base). L'API n'enregistre rien pour Knowledge Base en S0.
- Beat (S8) : trois entrées, `knowledge_base.work.dispatch` (`KB_WORK_DISPATCH_INTERVAL_SECONDS`), `knowledge_base.work.recover` (`KB_WORK_RECOVER_INTERVAL_SECONDS`) et `knowledge_base.work.reconcile` (`KB_RECONCILE_INTERVAL_SECONDS`, 300 s par défaut). Les trois vont sur la file par défaut. `reconcile` ne réconcilie pas elle-même : elle enregistre un work item `reconcile_knowledge_base` par créneau (un beat redémarré ou en double dans le même créneau désigne le même item), et aucun tant qu'un cycle attend ou tourne (revue S8), que `worker_ingestion` exécute entre deux traitements. Le scheduler reçoit les variables `KB_*` de `compose.yaml` : c'est lui qui lit la période.
- `--max-tasks-per-child` (S8, `worker_ingestion` seulement) : l'enfant est remplacé toutes les 50 tâches par défaut, parce que le pic de RSS d'un même processus croît sur des conversions PDF successives (mesure de la revue S6, [ADR 0012, suivi](../../../docs/decisions/0012-original-document-storage-and-purge.md)) ; le nouvel enfant recharge E5 (~4 s) et Docling PDF (~5 s) à leur premier usage (chargement paresseux). Les cycles de réconciliation comptent dans ces 50 tâches : à vide, 12 par heure, un enfant remplacé toutes les ~4 h 10 min sans rien recharger tant qu'aucun traitement n'arrive ; défaut conservé (revue S8, M9). Vérification : `docker compose exec worker_ingestion celery -A runtime.worker:celery_app inspect stats` (`"max-tasks-per-child": 50`).

Les Bounded Contexts sont enregistrés ici par leur surface publique uniquement (`backend/tests/architecture/`) : points d'entrée réexportés par leur package racine (`register`, `register_worker`, fabriques de contrats), voir [005 — Surface publique](../../../docs/conception/005-project-structure.md#surface-publique-dun-contexte-et-composition-root).

## Recherche de Knowledge Base dans l'API (S5)

- Le modèle d'embeddings (`intfloat/multilingual-e5-base`) est chargé par l'API à la **première recherche**, dans un thread, puis gardé par le processus : le démarrage et `/health/ready` n'en dépendent pas. Mesuré le 2026-11-18 sur la stack de dev : premier appel 4,3 s, RSS du processus serveur 146 → 1 267 Mio. Volume `models_cache` monté dans `api` ; `HF_HUB_DISABLE_XET=1` vient de l'environnement commun de `compose.yaml`.
- Pour fournir le contrat `KnowledgeSearch` à un autre contexte, `runtime` appellera `knowledge_base.build_knowledge_search(app.state.knowledge_base)` une fois, dans le `lifespan` après la création des ressources, et passera la même instance au consommateur ([005](../../../docs/conception/005-project-structure.md#transmission-dun-contrat-public-à-un-autre-contexte)) ; aucun consommateur n'existe en S5. Le modèle est celui des ressources, déjà partagé avec la route de recherche : il n'est chargé qu'une fois par processus (vérifié par `tests/unit/knowledge_base/test_build_knowledge_search.py`).
