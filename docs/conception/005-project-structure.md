# 005 — Structure du projet, DDD et Clean Architecture

## Inspiration vérifiée

`salletti/trading-agent-ai` a des Bounded Contexts de premier niveau sous `src/`, avec `domain`, `use_cases`, `adapters`, parfois `contracts` et `frameworks`. `runtime` est le composition root ; `shared` est réduit. `docs/` distingue `context`, `conception`, `decisions`.

Nous reprenons ces conventions, **pas le code métier trading**.

## Structure cible (à créer par vertical slices)

```text
src/
  runtime/                       # composition root, FastAPI, worker bootstrap
  interview_training/
    README.md
    domain/
      entities/
      value_objects/
      events/
      exceptions/
      providers/                # ports métier
    use_cases/
      start_training_session/
      present_question/
      submit_answer/
      record_assessment_result/
    adapters/
      controller/
        rest/
        messaging/
      gateway/
        persistence/
        messaging/
    contracts/                  # DTO publics stables si utiles
    frameworks/                 # config propre si justifiée
  assessment/
    README.md
    domain/
      entities/
      value_objects/
      events/
      exceptions/
      providers/
    use_cases/
      request_assessment/
      start_assessment_attempt/
      publish_assessment/
    adapters/
      controller/
        messaging/
        rest/
      gateway/
        persistence/
        llm/
        messaging/
    contracts/
    frameworks/
  shared/
    domain/                     # shared kernel minimal
  knowledge_base/               # envisagé
  learning_progress/            # envisagé
  question_bank/                # envisagé
tests/
  unit/
  integration/
  contract/
```

## Dépendances et conventions

- `domain` : Python pur, sans FastAPI, SQLAlchemy, Celery, LiteLLM ou RabbitMQ.
- `use_cases` : orchestration des ports, Unit of Work et application policies.
- `adapters/controller` : API HTTP, consommateur de message, CLI ; conversion vers commandes.
- `adapters/gateway` : SQLAlchemy, RabbitMQ, LLM, recherche vectorielle.
- `contracts` : façade publique inter-contextes, schémas versionnés dédiés aux Integration Events.
- `frameworks` : configuration propre au contexte si justifiée, sinon absent.
- `runtime` : composition root / wiring, jamais de logique métier.
- Aucun ORM partagé ni jointure métier inter-contextes.
- Python packages en snake_case ; classes en PascalCase.

## Différence intentionnelle avec trading-agent-ai

Pour ce projet RabbitMQ, les événements d'intégration sont des schémas indépendants des Domain Events Python. Dans trading-agent-ai, des Domain Events sont directement importables par les autres contextes. La distinction ici réduit le couplage aux détails internes et facilite les tests de compatibilité.

## Vertical slices proposés

1. Session + question statique ; tests invariants.
2. SubmitAnswer + base + outbox.
3. Consommation AnswerSubmitted + inbox + Assessment REQUESTED.
4. Worker LLM, validation, publication et outbox.
5. Projection, endpoint de lecture, polling/SSE.

Ne pas créer tous les dossiers vides par anticipation.
