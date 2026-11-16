# 004 — Contrats d'intégration V1

## Principes

Les Domain Events Python internes ne sont pas les Integration Events publiés durablement. Contrats stables, JSON versionné, mapping explicite dans adapters/application. Aucune entité ORM entre contextes.

## Enveloppe commune

```json
{
  "event_id": "uuid",
  "event_type": "interview.answer_submitted",
  "event_version": 1,
  "occurred_at": "2026-11-16T10:00:00Z",
  "correlation_id": "uuid",
  "causation_id": "uuid",
  "producer": "interview_training",
  "payload": {}
}
```

- `event_id` : déduplication, stable en republication.
- `correlation_id` : traçabilité du workflow.
- `causation_id` : commande ou événement déclencheur.
- Ne jamais mettre de secrets, clés API ou fichiers audio volumineux dans RabbitMQ.

## AnswerSubmitted.v1

**Producteur :** Interview Training. **Consommateur :** Assessment.

```json
{
  "event_type": "interview.answer_submitted",
  "event_version": 1,
  "payload": {
    "session_id": "session-123",
    "turn_id": "turn-007",
    "submission_id": "submission-456",
    "question": {"id": "question-789", "version": 2, "text": "Pourquoi utiliser Outbox ?"},
    "answer": {"format": "TEXT", "content": "Pour éviter les pertes..."},
    "assessment_policy": {"rubric_id": "backend-interview", "rubric_version": 3},
    "submitted_at": "2026-11-16T10:00:00Z"
  }
}
```

Assessment reçoit le snapshot nécessaire, sans requête obligatoire vers les entités internes du producteur.

## AssessmentPublished.v1

**Producteur :** Assessment. **Consommateur :** Interview Training.

```json
{
  "event_type": "assessment.published",
  "event_version": 1,
  "payload": {
    "assessment_id": "assessment-321",
    "submission_id": "submission-456",
    "revision": 1,
    "score": "7.5",
    "max_score": "10",
    "feedback": "Bonne compréhension de l'atomicité.",
    "weaknesses": ["transaction_boundary"],
    "published_at": "2026-11-16T10:00:08Z"
  }
}
```

Le consumer vérifie submission_id et traite les numéros de révision idempotemment, même en cas de livraison désordonnée.

## AssessmentFailed.v1

**Proposé :** événement terminal distinct avec assessment_id, submission_id, failure_code, failed_at, retryable=false. Ne pas l'émettre pour un simple fallback Groq → Cerebras. Format et règles de relance à préciser.

## Communication synchrone

DTO public `AssessmentSummary` pour consulter l'état/résultat via interface applicative ; ne pas exposer les entités Assessment. En V1, une projection locale côté Interview Training peut suffire.

## Compatibilité

Évolution de schéma versionnée ; tests de contrats producteurs/consommateurs. Les exemples ci-dessus sont des propositions jusqu'à validation définitive des champs.
