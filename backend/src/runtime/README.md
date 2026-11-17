# Runtime

Composition root technique du monolithe. Aucune règle métier.

| Module | Rôle |
|---|---|
| `settings.py` | Pydantic Settings : hôtes, ports, secrets (`SecretStr`), délais |
| `app.py` | `create_app()` : FastAPI, `lifespan` (moteur SQLAlchemy, client Qdrant), routeurs |
| `database.py` | `AsyncEngine`, fabrique de sessions, dépendance « une `AsyncSession` par requête », sonde |
| `vector_store.py` | client `AsyncQdrantClient` et sonde |
| `messaging.py` | sonde RabbitMQ (Kombu dans un thread, bornée) |
| `worker.py` | application Celery, tâche technique `runtime.ping` |
| `health.py` | `/health/live`, `/health/ready` (PostgreSQL requis, Qdrant/RabbitMQ informatifs) |
| `system.py` | `/api/v1/system/info` (données non sensibles) |
| `logging_config.py` | logging standard, format unique |

Les Bounded Contexts seront enregistrés ici par leur surface publique uniquement (`backend/tests/architecture/`) : points d'entrée réexportés par leur package racine (`register`, `register_worker`, fabriques de contrats), voir [005 — Surface publique](../../../docs/conception/005-project-structure.md#surface-publique-dun-contexte-et-composition-root).
