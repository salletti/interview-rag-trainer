# 005 — Structure du projet, DDD et Clean Architecture

> **Statut :** Proposed (suit l'ADR 0003, lui-même Proposed).
>
> Ce document fixe l'organisation du code : le niveau du monorepo (contextes,
> `runtime`, `shared`), puis la structure **à l'intérieur** de chaque Bounded
> Context.

## Inspiration vérifiée

`salletti/trading-agent-ai` a des Bounded Contexts de premier niveau sous `src/`, avec `domain`, `use_cases`, `adapters`, parfois `contracts` et `frameworks`. `runtime` est le composition root ; `shared` est réduit. `docs/` distingue `context`, `conception`, `decisions`. Les modules `strategy/`, `portfolio/` et `decision/` ont servi de référence pour la structure interne décrite plus bas.

Nous reprenons ces conventions, **pas le code métier trading**. Les écarts sont listés en fin de document.

## Vue d'ensemble

> **Mise à jour Phase 1 (2026-11-17, ADR 0006) :** le backend vit sous `backend/`, le frontend React sous `frontend/`.

```text
backend/src/
  runtime/                      # composition root : FastAPI, worker Celery, santé, settings
  interview_training/           # Bounded Context (ADR 0001)
  assessment/                   # Bounded Context (ADR 0001)
  shared/
    domain/                     # shared kernel minimal, si un besoin réel apparaît
  knowledge_base/               # envisagé
  learning_progress/            # envisagé
  question_bank/                # envisagé
backend/tests/
  unit/<contexte>/
  integration/<contexte>/       # contre compose.test.yaml
  contract/<contexte>/          # à venir avec les Integration Events
  architecture/                 # règles de dépendance (imports)
```

- `runtime` : composition root / wiring, jamais de logique métier. Il n'appelle que `register(app)` et `contracts/` d'un contexte.
- `shared` : primitives transverses justifiées uniquement ; jamais de modèle métier global.
- Aucun contexte n'importe `runtime`. Aucun ORM partagé ni jointure métier inter-contextes.
- Python packages en snake_case ; classes en PascalCase.

## Structure d'un Bounded Context

### Règle d'or : on ne crée un dossier que lorsqu'on y met du code

L'arborescence ci-dessous est le **plan de référence**, pas un squelette à
générer. Un vertical slice n'ajoute que les dossiers dont il a besoin. Un
`contracts/` ou un `frameworks/` absent est normal tant que rien ne le
justifie.

### Arborescence de référence

```text
backend/src/<contexte>/
    __init__.py                 # surface publique : register(app), et rien d'autre
    README.md                   # objectif, responsabilités, use cases, événements, contrats

    domain/                     # cœur métier, Python pur
        entities/               # agrégats et entités internes
        value_objects/
        events/                 # Domain Events LOCAUX (dataclasses), jamais exportés
        exceptions/             # erreurs métier du contexte
        services/               # règles métier sans état propre à une entité
        providers/              # ports (Protocol) : repositories, unit of work, horloge…

    use_cases/                  # un dossier par cas d'usage
        <verbe_objet>/
            <verbe_objet>_command.py    # class <VerbeObjet> (intention, dataclass figée)
            <verbe_objet>_handler.py    # class <VerbeObjet>Handler
            <verbe_objet>_result.py     # class <VerbeObjet>Result (si un retour est nécessaire)

    adapters/
        controller/             # entrées : traduisent un appel externe en commande
            __init__.py         # def register(app) : monte les routes, branche les consumers
            rest/               # routeurs FastAPI, schémas HTTP (Pydantic)
            messaging/          # consumers d'Integration Events → commandes locales (ACL)
            contracts/          # implémentations des providers exposés dans contracts/
        gateway/                # sorties : implémentent les ports de domain/providers
            persistence/        # repositories SQLAlchemy, unit of work
            messaging/          # écriture outbox, mapping Domain Event → Integration Event
            llm/                # (Assessment) moteur d'évaluation via LiteLLM
            vector_store/       # (contexte RAG) accès Qdrant

    contracts/                  # API publique du contexte, seule importable par un autre contexte
        events/                 # schémas d'Integration Events versionnés (AnswerSubmitted.v1…)
        result/                 # DTO publics en lecture (ex. AssessmentSummary)
        providers/              # Protocol exposés à d'autres contextes, si un besoin réel apparaît

    frameworks/                 # détails techniques propres au contexte
        sqlalchemy/             # tables + MetaData du contexte (importé par alembic/env.py)
        configuration/          # réglages techniques propres au contexte (si justifiés)
```

### Rôle et règles de chaque couche

| Couche | Contient | Peut importer | Ne doit jamais importer |
|---|---|---|---|
| `domain/` | agrégats, VO, Domain Events locaux, exceptions, services, ports | `shared.domain`, bibliothèque standard | FastAPI, SQLAlchemy, Celery, Kombu, Qdrant, LiteLLM, Pydantic Settings ; `use_cases`, `adapters`, `frameworks` |
| `use_cases/` | commandes, handlers, résultats ; orchestration et Unit of Work | `domain/`, `shared.domain` | frameworks ; `adapters/`, `frameworks/` |
| `adapters/controller/` | REST, consumers, implémentations de contrats | `use_cases/`, `domain/`, FastAPI, Pydantic | les couches internes d'un autre contexte |
| `adapters/gateway/` | implémentations des ports | `domain/`, `frameworks/`, SQLAlchemy, Qdrant, LiteLLM… | `use_cases/` d'un autre contexte |
| `contracts/` | Integration Events versionnés, DTO publics, Protocol exposés | bibliothèque standard, Pydantic pour la validation des schémas | `domain/` d'un autre contexte |
| `frameworks/` | tables SQLAlchemy, `MetaData`, configuration technique | SQLAlchemy, Pydantic Settings | `use_cases/`, `adapters/` |

Les règles marquées dans la colonne de droite sont vérifiées par
`backend/tests/architecture/test_dependency_rules.py`.

#### `domain/`

- Les agrégats exposent des méthodes métier (`session.submit_answer()`), pas
  des setters. Les invariants de [003](003-aggregates-and-invariants.md)
  vivent ici et sont testés sans base ni réseau.
- Les **Domain Events** sont des dataclasses figées, collectées par
  l'agrégat et relues par le handler. Ils ne sortent jamais du contexte.
- Les **ports** (`providers/`) sont des `typing.Protocol` nommés d'après le
  besoin métier : `TrainingSessionRepository`, `UnitOfWork`, `Clock`,
  `EvaluationEngine` (ADR 0005). Le nom du sous-dossier peut suivre le
  vocabulaire (`providers/`, `gateways/`), mais il reste sous `domain/`.

#### `use_cases/`

- Un dossier par commande de [002](002-event-flows.md) :
  `start_training_session/`, `submit_answer/`, `request_assessment/`…
- Nommage : la commande porte le nom de l'intention (`SubmitAnswer`), le
  handler la suffixe (`SubmitAnswerHandler`), le résultat aussi
  (`SubmitAnswerResult`). C'est la correspondance directe avec les tableaux
  de 002 ; trading-agent-ai utilise `Request`/`Response` pour le même rôle.
- Le handler ouvre l'Unit of Work, charge l'agrégat, appelle le domaine,
  persiste, écrit l'outbox dans **la même transaction**, puis commit. Il ne
  garde jamais une transaction ouverte pendant un appel réseau (LLM).
- Une commande d'écriture accepte une clé d'idempotence quand 002 l'exige.
- Pas de logique métier complexe : si le handler décide, la règle doit
  descendre dans le domaine.
- Les lectures simples (projection, `AssessmentSummary`) peuvent être des
  *queries* dans le même format (`get_<objet>/`), sans passer par un agrégat.

#### `adapters/controller/`

- `rest/` : routeurs FastAPI. Les schémas HTTP (Pydantic) sont séparés des
  commandes : le routeur traduit, il ne décide rien.
- `messaging/` : consumers d'Integration Events. Chacun valide le schéma
  versionné, traduit l'événement en **commande locale** (couche
  anti-corruption de [001](001-bounded-contexts.md)), et passe par l'inbox.
- `__init__.py` expose `register(app)`, réexporté par le `__init__.py`
  racine du contexte : c'est le seul point d'entrée que `runtime` appelle.

#### `adapters/gateway/`

- `persistence/` : repositories SQLAlchemy et Unit of Work. Mapping explicite
  entre lignes et agrégats ; aucun objet ORM ne remonte dans le domaine.
- `messaging/` : transforme les Domain Events en Integration Events
  (`contracts/events/`) et les écrit dans l'outbox du contexte. La publication
  RabbitMQ elle-même relève du relais d'outbox, câblé par `runtime`.
- Les tâches Celery éventuelles d'un contexte sont des *controllers* de
  travail (entrée) qui appellent un handler. Ce ne sont pas des contrats.

#### `contracts/`

- Seule partie du contexte qu'un **autre contexte** a le droit d'importer.
  Le code interne du contexte n'importe jamais sa propre façade.
- `events/` : un fichier par Integration Event et par version
  (`answer_submitted_v1.py`), conforme à l'enveloppe de
  [004](004-integration-contracts.md). Un changement incompatible crée un
  `v2`, il ne modifie pas `v1`.
- `result/` : DTO publics en lecture seule, jamais une entité du domaine.

#### `frameworks/`

- `sqlalchemy/` : tables et `MetaData` **propres au contexte** (préfixe ou
  schéma PostgreSQL dédié, par exemple `interview_training.*`). Aucune
  jointure entre tables de deux contextes. `alembic/env.py` est le seul
  endroit qui importe plusieurs `MetaData`.
- `configuration/` : uniquement si le contexte a un réglage technique que
  `runtime.settings` ne doit pas porter.

### Tests

Les tests restent sous `backend/tests/`, rangés par contexte en miroir du
code :

```text
backend/tests/
    unit/<contexte>/domain/...        # invariants, sans I/O
    unit/<contexte>/use_cases/...     # handlers avec fakes des ports
    integration/<contexte>/...        # repositories, outbox, consumers (compose.test.yaml)
    contract/<contexte>/...           # schémas d'Integration Events producteur/consommateur
    architecture/                     # règles de dépendance (transverse)
```

### Exemple : Interview Training au premier vertical slice

```text
interview_training/
    __init__.py                         # from .adapters.controller import register
    README.md
    domain/
        entities/
            training_session.py         # TrainingSession (Aggregate Root)
            interview_turn.py           # InterviewTurn (entité interne)
        value_objects/
            question_snapshot.py
            session_policy.py
        events/
            training_session_started.py
            question_presented.py
        exceptions/
            session_not_active.py
        providers/
            training_session_repository.py   # Protocol
            unit_of_work.py                  # Protocol
    use_cases/
        start_training_session/
            start_training_session_command.py
            start_training_session_handler.py
            start_training_session_result.py
        present_question/
            ...
    adapters/
        controller/
            __init__.py                 # register(app)
            rest/
                training_session_router.py
                schemas.py
        gateway/
            persistence/
                sqlalchemy_training_session_repository.py
                sqlalchemy_unit_of_work.py
    frameworks/
        sqlalchemy/
            tables.py                   # metadata + tables du contexte
```

`contracts/events/answer_submitted_v1.py` et
`adapters/gateway/messaging/` n'apparaissent qu'au slice `SubmitAnswer`
(outbox). `adapters/controller/messaging/` n'apparaît qu'au slice qui
consomme `AssessmentPublished.v1`.

### Exemple : Assessment au slice d'évaluation

```text
assessment/
    __init__.py
    README.md
    domain/
        entities/        assessment.py, assessment_attempt.py
        value_objects/   rubric.py, criterion_result.py, assessment_revision.py
        events/          assessment_requested.py, assessment_attempt_started.py
        services/        score_calculator.py        # calcul déterministe de la note
        providers/       assessment_repository.py, evaluation_engine.py, unit_of_work.py
    use_cases/
        request_assessment/
        start_assessment_attempt/
        publish_assessment/
    adapters/
        controller/
            messaging/   answer_submitted_consumer.py   # AnswerSubmitted.v1 → RequestAssessment
        gateway/
            persistence/
            llm/         litellm_evaluation_engine.py
            messaging/   outbox_writer.py
    contracts/
        events/          assessment_published_v1.py, assessment_failed_v1.py
        result/          assessment_summary.py
    frameworks/
        sqlalchemy/      tables.py
```

## Écarts assumés avec trading-agent-ai

| Sujet | trading-agent-ai | Interview RAG Trainer | Raison |
|---|---|---|---|
| Événements entre contextes | Domain Events importables directement depuis `domain/events/` d'un autre contexte | Domain Events privés ; seuls les **Integration Events** versionnés de `contracts/events/` traversent | schémas indépendants des Domain Events Python : moins de couplage aux détails internes, tests de compatibilité possibles (ADR 0003) |
| Nommage des cas d'usage | `GenerateSignal` + `Request` / `Response` | `SubmitAnswer` + `Handler` / `Result` | aligné sur les tableaux de commandes de 002 |
| Publication | `domain/publishers/` (publication en mémoire) | outbox écrite par `adapters/gateway/messaging/` dans la transaction métier | ADR 0004 |
| Tests | `tests/<contexte>/` | `backend/tests/{unit,integration,contract}/<contexte>/` | séparation unit / intégration déjà en place |
| Vérification des règles | import-linter | `backend/tests/architecture/` (analyse `ast`) | ADR 0006 |

Repris tels quels : `domain` / `use_cases` / `adapters/{controller,gateway}` /
`contracts` / `frameworks`, ports sous `domain/providers/`, `register(app)`
comme unique point d'entrée vu par `runtime`, tables et `MetaData` par
contexte dans `frameworks/sqlalchemy/`, un dossier par cas d'usage, un
README par contexte.

## Vertical slices proposés

1. Session + question statique ; tests invariants.
2. SubmitAnswer + base + outbox.
3. Consommation AnswerSubmitted + inbox + Assessment REQUESTED.
4. Worker LLM, validation, publication et outbox.
5. Projection, endpoint de lecture, polling/SSE.

Ne pas créer tous les dossiers vides par anticipation.
