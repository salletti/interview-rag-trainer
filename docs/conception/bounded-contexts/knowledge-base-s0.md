# Knowledge Base — S0 : infrastructure de traitement durable

> **Statut : Proposed — prêt à implémenter.** Ce document précise la slice S0
> de la [feuille de route](knowledge-base-roadmap.md). Il applique le complément
> « work queue durable » de l'[ADR 0004](../../decisions/0004-reliable-messaging.md)
> (Accepted) et les points d'entrée de
> [005](../005-project-structure.md#surface-publique-dun-contexte-et-composition-root).
> Les valeurs numériques sont des **défauts Proposed**, configurables et à
> ajuster sur mesure.
>
> Références : [conception Knowledge Base](knowledge-base.md) §6–§7,
> [ADR 0006](../../decisions/0006-technical-bootstrap.md) (socle existant),
> [ADR 0007](../../decisions/0007-knowledge-base-bounded-context.md).

## Sommaire

1. [Objectif et périmètre](#1-objectif-et-périmètre)
2. [Schéma de `knowledge_base.work_items`](#2-schéma-de-knowledge_basework_items)
3. [Transitions d'état](#3-transitions-détat)
4. [Commandes techniques](#4-commandes-techniques)
5. [Fencing et transactions](#5-fencing-et-transactions)
6. [Conditions exactes d'idempotence](#6-conditions-exactes-didempotence)
7. [Retry et backoff](#7-retry-et-backoff)
8. [Dispatch, messages Celery et scheduler](#8-dispatch-messages-celery-et-scheduler)
9. [Ressources asyncio dans un worker Celery prefork](#9-ressources-asyncio-dans-un-worker-celery-prefork)
10. [Points d'entrée et emplacement du code](#10-points-dentrée-et-emplacement-du-code)
11. [Configuration Docker Compose](#11-configuration-docker-compose)
12. [Stratégie de tests](#12-stratégie-de-tests)
13. [Critères d'acceptation](#13-critères-dacceptation)
14. [Risques](#14-risques)
15. [Mises à jour documentaires attendues](#15-mises-à-jour-documentaires-attendues)

---

## 1. Objectif et périmètre

S0 livre un mécanisme **générique** de travail durable propre à Knowledge
Base : enregistrer un travail dans la même transaction que l'état métier, le
faire exécuter par un worker Celery, survivre aux pertes de messages, aux
redélivrances et aux crashs, et terminer chaque travail dans un état
explicite.

**Dans le périmètre :**

- package `knowledge_base` minimal (points d'entrée, configuration, tables) ;
- schéma PostgreSQL `knowledge_base` et table `work_items` (migration Alembic) ;
- port d'enqueue, adapter SQL du cycle de vie, registre de handlers ;
- tâches Celery `execute`, `dispatch`, `recover` ;
- composition des ressources asyncio dans les workers prefork ;
- services Compose `worker_ingestion` et `scheduler`, volumes `kb_blobs` et
  `models_cache` ;
- extension des tests d'architecture.

**Hors périmètre (slices ultérieures) :** tout `kind` métier
(`extract_version`, `index_version`…), agrégats, endpoints REST, blob store,
Docling, SentenceTransformers, torch, collections Qdrant, Integration Events,
outbox d'événements, `RetryIngestion` (S8).

**Prérequis documentaires :** ADR 0007 et complément de l'ADR 0004 Accepted
(fait). ADR 0003 et document 005 restent Proposed : leur acceptation est
recommandée avant S0, sans être bloquante, car S0 applique leur contenu actuel.

## 2. Schéma de `knowledge_base.work_items`

| Colonne | Type | Contraintes | Rôle |
|---|---|---|---|
| `id` | `uuid` | PK | identifiant transporté par le message Celery |
| `kind` | `text` | NOT NULL | type de travail ; détermine le handler et la file |
| `payload` | `jsonb` | NOT NULL, défaut `{}` | identifiants uniquement ; jamais de contenu de fichier ni de secret |
| `dedup_key` | `text` | NOT NULL, `UNIQUE` | identité logique complète du travail, ex. `extract_version:<version_id>` |
| `status` | `text` | NOT NULL, `CHECK IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED')` | état |
| `attempts` | `integer` | NOT NULL, défaut 0, `>= 0` | nombre de claims effectués |
| `max_attempts` | `integer` | NOT NULL, `>= 1` | défaut par `kind` (5) |
| `fencing_token` | `bigint` | NOT NULL, défaut 0 | incrémenté à chaque claim et à l'annulation |
| `lease_owner` | `text` | NULL | `hostname:pid` du worker ayant fait le claim |
| `lease_expires_at` | `timestamptz` | NULL ; NOT NULL si `RUNNING` (`CHECK`) | fin du lease |
| `available_at` | `timestamptz` | NOT NULL, défaut `now()` | date à partir de laquelle l'item peut être claimé |
| `dispatched_at` | `timestamptz` | NULL | dernier envoi d'un message de réveil |
| `last_error_code` | `text` | NULL | code stable (`TRANSIENT`, `LEASE_EXPIRED`, `UNKNOWN_KIND`, codes métier…) |
| `last_error_message` | `text` | NULL, tronqué à 2 000 caractères | diagnostic, sans secret |
| `correlation_id` | `uuid` | NULL | traçabilité (enveloppe de 004) |
| `created_at` / `updated_at` | `timestamptz` | NOT NULL | horodatage |
| `completed_at` | `timestamptz` | NULL | date d'entrée dans un état terminal |

Index :

- `ix_work_items_dispatchable` sur `(available_at)` `WHERE status = 'PENDING'` ;
- `ix_work_items_running_lease` sur `(lease_expires_at)` `WHERE status = 'RUNNING'` ;
- `ix_work_items_kind_status` sur `(kind, status)` (observabilité).

Règles :

- Toutes les dates sont calculées par PostgreSQL (`now()`), jamais par
  l'horloge du worker : aucune dérive d'horloge entre processus.
- `dedup_key` est unique **pour toujours**, y compris pour les items
  terminés. Un nouveau travail logique a donc une clé différente (par exemple
  `sync_visibility:<document_id>:<visibility_revision>`). Une relance manuelle
  (S8) rouvre l'item existant au lieu d'en créer un autre.
- La table appartient au schéma `knowledge_base` (`MetaData(schema=…)` dans
  `frameworks/sqlalchemy/`). La table de versions Alembic reste dans `public`.
- Purge des items terminés : **[Open]**, non nécessaire en V1 (volume faible).

## 3. Transitions d'état

```mermaid
stateDiagram-v2
  [*] --> PENDING: enqueue (transaction métier)
  PENDING --> RUNNING: claim (attempts+1, token+1, lease)
  RUNNING --> RUNNING: heartbeat (token identique, lease prolongé)
  RUNNING --> SUCCEEDED: complete (token identique)
  RUNNING --> PENDING: retry_later (erreur transitoire, attempts < max)
  RUNNING --> FAILED: fail (erreur terminale, ou attempts >= max)
  RUNNING --> PENDING: recover (lease expiré, attempts < max)
  RUNNING --> FAILED: recover (lease expiré, attempts >= max)
  PENDING --> CANCELLED: cancel (token+1)
  RUNNING --> CANCELLED: cancel (token+1)
  FAILED --> PENDING: reopen (S8, RetryIngestion)
```

| Transition | Condition SQL (`WHERE`) | Effets |
|---|---|---|
| enqueue | `INSERT … ON CONFLICT (dedup_key) DO NOTHING` | `PENDING`, `available_at = now()` ; renvoie l'item existant en cas de conflit |
| claim | `id = :id AND status = 'PENDING' AND available_at <= now()` | `RUNNING`, `attempts+1`, `fencing_token+1`, `lease_owner`, `lease_expires_at = now() + ttl` |
| heartbeat | `id = :id AND status = 'RUNNING' AND fencing_token = :t` | `lease_expires_at = now() + ttl` |
| complete | `id = :id AND status = 'RUNNING' AND fencing_token = :t` | `SUCCEEDED`, `completed_at`, lease effacé |
| retry_later | idem + `attempts < max_attempts` | `PENDING`, `available_at = now() + backoff(attempts)`, `dispatched_at = NULL`, erreur enregistrée |
| fail | `id = :id AND status = 'RUNNING' AND fencing_token = :t` | `FAILED`, `completed_at`, erreur enregistrée |
| recover | `status = 'RUNNING' AND lease_expires_at < now()` | `attempts < max` → `PENDING` (+ backoff, `LEASE_EXPIRED`) ; sinon `FAILED` (`LEASE_EXPIRED_MAX_ATTEMPTS`) |
| cancel | `id = :id AND status IN ('PENDING','RUNNING')` | `CANCELLED`, `fencing_token+1`, `completed_at` |
| reopen (S8) | `id = :id AND status = 'FAILED'` | `PENDING`, `attempts = 0`, `available_at = now()` ; erreur conservée |

Une transition dont l'UPDATE touche zéro ligne n'est **jamais** une erreur
technique : elle signifie « déjà traité, annulé ou périmé ». L'appelant
journalise et s'arrête.

## 4. Commandes techniques

| Commande | Appelant | Transaction | Rôle |
|---|---|---|---|
| `EnqueueWork(kind, payload, dedup_key, correlation_id?)` | handler métier, via le port `WorkQueue` | **celle du handler métier** (même Unit of Work) | enregistre le travail atomiquement avec l'état métier |
| `CancelWork(dedup_key or id)` | handler métier | celle du handler métier | invalide un travail devenu inutile (ex. version `DISCARDED`) |
| `DispatchPendingWork(batch)` | tâche périodique `dispatch` | propre, courte | sélectionne et marque les items à réveiller, publie après commit |
| `ClaimWorkItem(id, owner)` | tâche `execute` | propre, courte | lease + fencing |
| `HeartbeatWorkItem(id, token)` | handler long | propre, courte | prolonge le lease |
| `CompleteWorkItem(id, token)` | handler | **celle de la finalisation métier** | succès atomique avec les effets métier |
| `RetryWorkItemLater(id, token, error)` | tâche `execute` | propre | erreur transitoire |
| `FailWorkItem(id, token, error)` | tâche `execute` | celle de la finalisation d'échec métier, si le handler en fournit une | échec terminal |
| `RecoverExpiredLeases(batch)` | tâche périodique `recover` | propre, courte | reprise après crash |

Seuls `EnqueueWork` et `CancelWork` sont exposés au domaine, par le port
`domain/providers/work_queue.py` (Protocol). Les autres sont des opérations
d'adapter, utilisées par les tâches Celery.

## 5. Fencing et transactions

Déroulé d'une exécution (tâche `execute(work_item_id)`) :

```text
T1  claim                → COMMIT       (retour : token, ou None → fin silencieuse)
    handler.execute(item, ctx)          hors transaction : effets externes idempotents,
                                         heartbeats éventuels
T2  finalisation          → COMMIT       écritures métier + complete(id, token)
                                         0 ligne sur complete → ROLLBACK de T2 entier
en cas d'exception :
T2' retry_later | fail    → COMMIT       (+ hook métier d'échec terminal dans la même transaction)
```

Contrat d'un handler (`adapters/controller/tasks/`) :

```python
class WorkHandler(Protocol):
    kind: ClassVar[str]
    queue: ClassVar[str]          # ex. "knowledge_base.ingestion"
    max_attempts: ClassVar[int]

    async def execute(self, item: ClaimedWorkItem, ctx: WorkContext) -> None:
        """Effets externes idempotents, puis ctx.finalize(callback) :
        callback(uow) écrit l'état métier ; ctx ajoute complete(id, token)
        dans la même transaction et lève StaleAttempt si 0 ligne."""

    async def on_terminal_failure(self, uow: UnitOfWork, item: ClaimedWorkItem,
                                  error: FailureReason) -> None:
        """Facultatif : écrit l'échec métier (ex. version FAILED) dans la
        transaction de fail()."""
```

- Classification des erreurs : `TerminalWorkError(code)` → `fail` ;
  `TransientWorkError` et toute exception inattendue → `retry_later`
  (comptée dans `attempts`) ; `StaleAttempt` → journalisée, aucune transition.
- Une transaction n'est **jamais** ouverte pendant le travail hors
  transaction (règle de 002).
- Le handler ne fait aucune écriture métier PostgreSQL en dehors de
  `ctx.finalize` : c'est la condition pour que le fencing protège l'état métier.

## 6. Conditions exactes d'idempotence

**La redélivrance d'un message ne garantit pas que l'effet d'un handler ne
s'exécute qu'une seule fois.** Un même item peut être exécuté plusieurs fois :

1. message de réveil envoyé deux fois (redispatch après `redispatch_after`) ;
2. redélivrance RabbitMQ (connexion perdue avant l'acquittement) ;
3. lease expiré pendant qu'une tentative lente est **toujours active**
   (tentative zombie), puis nouveau claim ;
4. crash après les effets externes, mais avant T2.

Les cas 1 et 2 sont absorbés par le claim conditionnel : le second message
trouve l'item `RUNNING` ou terminé et s'arrête. Les cas 3 et 4 provoquent une
**réexécution réelle** du handler.

Garanties qui en résultent :

| Effet | Garantie | Condition |
|---|---|---|
| État métier PostgreSQL | **au plus une** finalisation réussie par item, et **au moins une** issue terminale | écritures métier uniquement dans `ctx.finalize` (fencing) |
| Effet externe (Qdrant, fichier) | **au moins une** exécution si l'item réussit ; **plusieurs** possibles | doit satisfaire les conditions ci-dessous |

Un effet externe est idempotent **si et seulement si** :

1. son identité est déterministe et dérivée de l'item (pas d'identifiant
   aléatoire généré pendant l'exécution) ;
2. il écrase sans accumuler (upsert, écriture atomique par renommage) ;
3. une suppression est exprimée par un filtre déterministe ;
4. le résultat est déterministe à entrée égale (une variation numérique
   négligeable d'embedding est acceptée) ;
5. une tentative zombie produisant l'effet **après** une finalisation plus
   récente laisse un état équivalent ou un état non visible, corrigé par la
   réconciliation (cf. [knowledge-base.md §7.3](knowledge-base.md#73-conditions-didempotence-des-effets-externes)).

Un effet qui ne respecte pas ces conditions (envoi de mail, appel facturé,
compteur) **ne doit pas** être placé dans un handler de `work_items` sans
mécanisme supplémentaire (clé d'idempotence côté destinataire). Aucun cas de
ce type n'existe dans Knowledge Base V1.

## 7. Retry et backoff

| Paramètre | Défaut Proposed | Variable |
|---|---|---|
| `max_attempts` | 5 (par `kind`) | déclaré par le handler |
| Base du backoff | 10 s | `KB_WORK_BACKOFF_BASE_SECONDS` |
| Plafond | 900 s | `KB_WORK_BACKOFF_MAX_SECONDS` |
| Formule | `d = min(plafond, base × 2^(attempts−1))`, puis délai = `d/2 + U(0, d/2)` (*equal jitter*) | — |
| `lease_ttl` | 300 s | `KB_WORK_LEASE_TTL_SECONDS` |
| Heartbeat | toutes les `lease_ttl / 3` pour un handler long | `ctx.heartbeat()` |
| Limite Celery dure (`time_limit`) | 3 600 s | `KB_WORK_TIME_LIMIT_SECONDS` |

- La fonction de backoff est **pure** (horloge et aléa injectés) et testée
  unitairement.
- Un dépassement de `time_limit` tue le processus enfant : le lease expire et
  `recover` prend le relais, comme pour un OOM.
- Un CPU long (Docling, embeddings) exécuté dans la boucle asyncio bloquerait
  le heartbeat : le travail CPU doit tourner dans `asyncio.to_thread` pendant
  que la boucle envoie les heartbeats.

## 8. Dispatch, messages Celery et scheduler

### 8.1 Messages

- Une seule tâche d'exécution : `knowledge_base.work.execute(work_item_id)`.
  Le message ne contient **que** l'identifiant ; il est routé vers la file du
  `kind` (`knowledge_base.ingestion` pour les traitements CPU).
- **Acquittement à la réception** (`acks_late = False`) **[Proposed]** : le
  message n'est qu'un signal de réveil, la durabilité est portée par la table.
  Ce choix évite le `consumer_timeout` de RabbitMQ (30 minutes par défaut) sur
  les tâches longues et les redélivrances massives. Un message perdu, ou un
  worker mort après l'acquittement, est rattrapé par `redispatch_after` ou par
  `recover`.
- `worker_prefetch_multiplier = 1` ; sérialisation JSON (inchangée, ADR 0006).

### 8.2 Dispatch

Tâche périodique `knowledge_base.work.dispatch` :

1. Transaction courte : `SELECT id, kind FROM work_items WHERE status =
   'PENDING' AND kind = ANY(:registered_kinds) AND available_at <= now()
   AND (dispatched_at IS NULL OR dispatched_at < now() - :redispatch_after)
   ORDER BY available_at LIMIT
   :batch FOR UPDATE SKIP LOCKED`, en respectant le plafond en vol par file
   (ci-dessous) ; `UPDATE … SET dispatched_at = now()` ; **COMMIT**.
2. **Après le commit**, publication d'un message par item.
3. Un crash entre 1 et 2 est rattrapé au passage suivant après
   `redispatch_after`.
4. **Seuls les `kind` enregistrés sont dispatchés.** Un item dont le `kind`
   n'a pas encore de handler (par exemple `extract_version` entre S2 et S3)
   reste `PENDING` sans consommer de tentative. Si un message arrive malgré
   tout pour un `kind` inconnu, `execute` le termine en `FAILED`
   (`UNKNOWN_KIND`).

**Plafond en vol par file** : `en_vol = RUNNING + PENDING dispatchés depuis
moins de redispatch_after`. On ne dispatche que si `en_vol < max_in_flight`.
Cela borne la longueur de la file RabbitMQ quand le worker d'ingestion est
occupé par une tâche longue, et limite les doublons de réveil (toujours
inoffensifs grâce au claim).

### 8.3 Reprise

Tâche périodique `knowledge_base.work.recover` : applique la transition
`recover` (§3) par lots `FOR UPDATE SKIP LOCKED`.

### 8.4 Paramètres

| Paramètre | Défaut Proposed | Variable |
|---|---|---|
| Intervalle de dispatch | 2 s | `KB_WORK_DISPATCH_INTERVAL_SECONDS` |
| Taille de lot | 50 | `KB_WORK_DISPATCH_BATCH` |
| `redispatch_after` | 600 s | `KB_WORK_REDISPATCH_AFTER_SECONDS` |
| `max_in_flight` (file d'ingestion) | 2 | `KB_WORK_MAX_IN_FLIGHT` |
| Intervalle de reprise | 30 s | `KB_WORK_RECOVER_INTERVAL_SECONDS` |

### 8.5 Scheduler

- Service `scheduler` = **Celery beat**, une seule instance (contrainte
  documentée). Il ne fait qu'émettre `dispatch` et `recover` ; il n'accède pas
  à la base.
- `dispatch` et `recover` sont routées vers la **file par défaut**
  (`interview_rag_trainer.default`, worker existant), jamais vers la file
  d'ingestion : elles ne doivent pas attendre derrière une extraction longue.
- Fichier d'état de beat éphémère (`/tmp`) : les planifications sont des
  intervalles, rien n'est à conserver.
- Délai maximal de prise en charge d'un item, sans panne :
  `intervalle de dispatch + attente en file`. Avec perte d'un message :
  `+ redispatch_after`. Après crash d'un worker : `+ lease_ttl + intervalle de
  reprise`.

## 9. Ressources asyncio dans un worker Celery prefork

Les handlers et repositories sont asynchrones (SQLAlchemy asyncio + asyncpg) ;
Celery prefork exécute des tâches synchrones dans des processus enfants.

| Moment | Processus | Action |
|---|---|---|
| Import de `runtime.worker` | maître | création de l'application Celery, appel de `knowledge_base.register_worker(celery_app, resources)` : enregistrement des tâches, des routes et du `beat_schedule`. **Aucune connexion ni boucle créée.** |
| Signal `worker_process_init` | chaque enfant (après fork) | création d'**une** boucle `asyncio.new_event_loop()`, puis du moteur asyncpg et de la fabrique de sessions **sur cette boucle** (pool réduit : `pool_size=2`) |
| Exécution d'une tâche | enfant | `loop.run_until_complete(coroutine)` sur la boucle persistante |
| Signal `worker_process_shutdown` | enfant | `engine.dispose()`, fermeture de la boucle |

- `resources` est un **fournisseur paresseux** (`Callable[[], KnowledgeBaseWorkerResources]`)
  défini par Knowledge Base : la registration a lieu dans le maître, les
  ressources n'existent que dans les enfants.
- Interdits : `asyncio.run()` par tâche (nouvelle boucle à chaque fois, pool
  asyncpg lié à une boucle fermée) ; création du moteur avant le fork ;
  pool `threads` ou `gevent` (boucle partagée entre threads).
- Pools supportés : `prefork` en exploitation, `solo` dans les tests
  in-process (les ressources sont alors fournies explicitement par le test).
- Le worker d'ingestion tourne avec `--concurrency=1` : un seul modèle chargé
  en mémoire à partir de S3/S4.

## 10. Points d'entrée et emplacement du code

Conformes à [005](../005-project-structure.md#surface-publique-dun-contexte-et-composition-root)
et au précédent de `trading-agent-ai` (fabriques réexportées par le package
racine depuis `adapters/controller/dependencies`).

```text
backend/src/knowledge_base/
  __init__.py                       # réexporte register_worker, KnowledgeBaseWorkerResources,
                                    # KnowledgeBaseSettings (et register quand S2 ajoutera des routes)
  README.md
  domain/providers/work_queue.py    # Protocol WorkQueue : enqueue, cancel
  adapters/
    controller/
      dependencies.py               # register_worker(celery_app, resources)
      tasks/
        execution.py                # tâche execute + WorkContext
        dispatch.py                 # tâches dispatch et recover
        registry.py                 # registre kind → WorkHandler (vide en production en S0)
        retry_policy.py             # backoff et classification, purs
    gateway/persistence/
      sqlalchemy_work_queue.py      # transitions SQL (§3)
  frameworks/
    sqlalchemy/tables.py            # MetaData(schema="knowledge_base"), work_items
    configuration/settings.py       # KnowledgeBaseSettings (pydantic-settings, préfixe KB_)
```

Côté `runtime` : `runtime/worker.py` appelle `knowledge_base.register_worker`
et gère les signaux du §9 ; `alembic/env.py` ajoute le `MetaData` de
Knowledge Base à `target_metadata` avec `include_schemas=True`. `runtime/app.py`
n'enregistre rien pour Knowledge Base en S0 (aucune route).

## 11. Configuration Docker Compose

Changements à apporter dans S0 (rien n'est modifié par ce document) :

| Élément | `compose.yaml` | `compose.test.yaml` |
|---|---|---|
| `worker` (existant) | inchangé, consomme `interview_rag_trainer.default` ; exécute `dispatch` et `recover` | inchangé |
| `worker_ingestion` (nouveau) | même image ; `celery -A runtime.worker:celery_app worker -Q knowledge_base.ingestion --concurrency=1 --prefetch-multiplier=1` ; dépend de `migrate` et `rabbitmq` ; healthcheck `inspect ping` | non requis : les tests exécutent un worker in-process |
| `scheduler` (nouveau) | même image ; `celery -A runtime.worker:celery_app beat --schedule /tmp/celerybeat-schedule` ; une seule instance | non requis |
| Volume `kb_blobs` | monté dans `api`, `worker`, `worker_ingestion` sur `/var/lib/interview-rag-trainer/blobs` | tmpfs ou volume éphémère si un test l'exige |
| Volume `models_cache` | monté dans `api` et `worker_ingestion` sur `/var/cache/interview-rag-trainer/models` (utilisé à partir de S3/S4) | — |
| Image (`backend/Dockerfile`) | création des répertoires de montage avec propriétaire `app` (un volume nommé neuf copie les droits du répertoire de l'image) | idem |
| `.env.example` | variables `KB_*` documentées avec leurs défauts | — |

Aucun nouveau service d'infrastructure (pas de Redis, pas de MinIO). Celery
beat est un processus de la même image, pas une infrastructure.

## 12. Stratégie de tests

### 12.1 Unitaires (sans I/O)

| Test | Vérifie |
|---|---|
| `test_backoff_*` | croissance exponentielle, plafond, bornes du jitter (aléa injecté) |
| `test_error_classification_*` | terminal → fail ; transitoire et exception inconnue → retry ; dernière tentative → fail |
| `test_recover_decision_*` | lease expiré avec ou sans tentatives restantes |
| `test_registry_*` | `kind` inconnu à l'exécution → échec terminal `UNKNOWN_KIND` ; double enregistrement refusé |
| `test_settings_*` | défauts `KB_*`, validation (valeurs > 0, cohérence `heartbeat < lease_ttl`) |

### 12.2 Intégration (PostgreSQL et RabbitMQ réels, `compose.test.yaml`)

Le worker est démarré **dans le processus de test** (`celery.contrib.testing`,
pool `solo`) avec un registre contenant un handler de test : le broker et la
base sont réels, le handler est un artefact de test qui n'existe pas dans le
code de production.

| # | Scénario | Attendu |
|---|---|---|
| 1 | enqueue puis rollback de la transaction métier | aucune ligne |
| 2 | enqueue puis commit | une ligne `PENDING` |
| 3 | deux enqueue avec la même `dedup_key` | une seule ligne ; le second renvoie l'item existant |
| 4 | cycle complet : enqueue → dispatch → execute → finalisation | `SUCCEEDED` ; effet métier de test écrit une fois |
| 5 | même message livré deux fois | un seul claim ; un seul effet métier |
| 6 | lease expiré pendant que la tentative A s'exécute ; recover puis claim B ; A tente de finaliser | finalisation de A rejetée (0 ligne, rollback) ; B finalise ; un seul effet métier |
| 7 | item dispatché, message jamais consommé | redispatché après `redispatch_after` (horloge accélérée par paramètre) |
| 8 | handler levant une erreur transitoire | `PENDING`, `available_at` repoussé, `attempts` incrémenté ; après `max_attempts` : `FAILED`, `last_error_*` renseigné |
| 9 | handler levant une erreur terminale avec hook métier | `FAILED` et écriture métier d'échec dans la même transaction |
| 10 | annulation pendant l'exécution | finalisation rejetée ; état `CANCELLED` |
| 11 | plafond `max_in_flight` | pas plus de N items dispatchés en vol |
| 11b | item d'un `kind` non enregistré | jamais dispatché, reste `PENDING`, `attempts = 0` |
| 12 | migration : `upgrade head`, `downgrade -1`, `upgrade head` | sans erreur ; schéma conforme au §2 |
| 13 | ressources prefork | une tâche exécutée par un vrai processus enfant réutilise la même boucle et le même moteur sur deux tâches successives (pas d'erreur « attached to a different loop ») |

### 12.3 Architecture

- `bounded_contexts()` retourne `assessment`, `interview_training`,
  `knowledge_base` ; le test `test_src_contains_the_validated_contexts` est
  mis à jour en conséquence (ADR 0007 Accepted).
- `FRAMEWORKS` est étendu : `docling`, `docling_core`, `sentence_transformers`,
  `transformers`, `torch`, `numpy` (ADR 0008), avec un test de détection de
  violation pour au moins `docling` et `sentence_transformers`.
- `runtime` n'importe que `knowledge_base` (racine) et `knowledge_base.contracts`
  (règle existante `runtime_only_sees_public_surface`).
- `celery` reste interdit dans `domain/` et `use_cases/` (règle existante).

### 12.4 Vérifications Compose

- `docker compose up --build -d` : `worker_ingestion` et `scheduler` démarrés,
  `worker_ingestion` *healthy* ;
- `celery inspect active_queues` montre `knowledge_base.ingestion` consommée
  par `worker_ingestion` uniquement ;
- écriture d'un fichier dans `kb_blobs` par l'utilisateur `app` depuis `api` et
  `worker_ingestion`.

## 13. Critères d'acceptation

1. `make ci` et `make test-integration` passent.
2. Les scénarios d'intégration 1 à 13 passent contre PostgreSQL et RabbitMQ
   réels.
3. **Aucun travail commité n'est perdu** : il atteint `SUCCEEDED`, `FAILED` ou
   `CANCELLED` malgré la perte d'un message (7) ou la mort d'un worker (6).
4. **Aucun effet métier PostgreSQL n'est appliqué deux fois** malgré une
   livraison at-least-once (5, 6, 10).
5. Aucune transaction n'est ouverte pendant l'exécution d'un handler (revue de
   code et test 6).
6. `docker compose up --build -d` démarre les nouveaux services ;
   `/health/ready` est inchangé.
7. Aucune dépendance Docling, SentenceTransformers ou torch ajoutée ; aucun
   endpoint, agrégat ou `kind` métier.
8. Documentation mise à jour (§15).

## 14. Risques

| Risque | Mitigation |
|---|---|
| Boucle asyncio et asyncpg dans Celery prefork | §9 ; test 13 avec un vrai processus enfant |
| Deux instances de beat (doublons de dispatch) | inoffensif grâce au claim et à `SKIP LOCKED` ; une seule instance documentée |
| Permissions des volumes nommés pour l'utilisateur non-root `app` | répertoires créés dans l'image ; vérification Compose |
| Tâche longue bloquant le heartbeat | travail CPU dans `asyncio.to_thread` (§7) |
| Accumulation de messages de réveil | plafond `max_in_flight` (§8.2) |
| Registre de handlers pollué par des besoins de test | handler de test défini sous `tests/`, injecté par le test |
| Généralisation prématurée vers `shared/` | mécanisme gardé dans Knowledge Base ; extraction vers `shared` seulement quand Assessment en aura besoin (règle de 005 sur `shared`) |

## 15. Mises à jour documentaires attendues

- `backend/src/knowledge_base/README.md` (créé avec le package) ;
- `backend/src/runtime/README.md` : registration du worker, signaux ;
- `docs/guides/environnement-local.md` : nouveaux services, volumes, variables `KB_*` ;
- [feuille de route](knowledge-base-roadmap.md) : S0 marqué terminé ;
- ADR 0006 : aucune modification de ses choix ; ajout éventuel d'une référence
  vers les nouveaux services dans « Conséquences » si le nombre de services
  annoncé change.
