# 003 — Modèle tactique : agrégats et invariants

## Interview Training — TrainingSession (Aggregate Root provisoire)

- `session_id`, `candidate_id`, `status`, `SessionPolicy`, `current_turn_id`, `version`.
- `InterviewTurn` : entité interne (sequence, QuestionSnapshot, PRESENTED/ANSWERED/SKIPPED).
- `Submission` : entité interne (id, réponse finale, horodatage).
- `AssessmentReference` : VO (submission_id, assessment_id, revision, disponibilité).
- `QuestionSnapshot` : VO figé (texte, version, consignes, provenance).

### Invariants

1. Soumission uniquement sur un tour existant, présenté et autorisé.
2. En mode séquentiel, une seule question active.
3. Une réponse finale logique par tour, sauf nouvelle tentative prévue par politique.
4. Réponse finale immuable, retries sans double écriture.
5. Séance COMPLETED/CANCELLED : aucune nouvelle soumission.
6. Résultat d'Assessment rattaché à la bonne submission_id.
7. Une révision ancienne ne remplace jamais une plus récente.
8. Clôture possible avant publication des corrections en mode différé.
9. Disponibilité du feedback distincte de sa visibilité.

États envisagés : CREATED, ACTIVE, PAUSED, COMPLETED, CANCELLED. La SessionPolicy contient mode INTERVIEW/QUIZ/EXAM, feedback IMMEDIATE/END_OF_SESSION, skips, nombre de questions, référence barème.

Une session très longue pourrait justifier InterviewTurn comme Aggregate Root indépendant : à revoir selon besoins, pas anticiper.

## Assessment — Assessment (Aggregate Root provisoire)

- Identité, référence externe de soumission, snapshot du contenu, Rubric et version, status, version optimiste.
- `AssessmentRevision` : révision métier publiée immuable (score, feedback, critères).
- `CriterionResult` : VO, plage/justification.
- `AssessmentAttempt` : tentative technique avec lease/fencing ; stockage autonome possible pour éviter un gros agrégat.
- Les appels individuels aux providers ne doivent pas créer plusieurs Assessments logiques.

États proposés : REQUESTED, EVALUATING, PUBLISHED, FAILED, CANCELLED.

### Invariants

1. Score final dans [0, max_score] et calculé selon la version figée de Rubric.
2. Critères obligatoires complets et valides.
3. Aucun résultat invalide/incomplet n'est publié.
4. Une révision publiée est immuable.
5. Tentative périmée incapable de publier (fencing token).
6. Une soumission / policy ne produit qu'une évaluation initiale logique.
7. Provenance LLM, prompt, sources et paramètres tracée sans secrets.
8. Une réévaluation est une nouvelle révision, pas une modification silencieuse.

### Scoring à approfondir

- QCM unique : calcul déterministe.
- QCM multiple : `clamp(10 * correct_selected / correct_total - 10 * incorrect_selected / incorrect_total, 0, 10)`, avec règle spéciale à définir si incorrect_total = 0.
- Réponse libre : le LLM propose des niveaux par critère ; l'application valide et calcule la note finale.

Le modèle Assessment est réutilisable hors Interview Training.

## Knowledge Base

Les agrégats (`KnowledgeDocument`, `KnowledgeCorpus`, `Indexation`), leurs états et leurs invariants sont décrits dans [knowledge-base.md §3–§5](bounded-contexts/knowledge-base.md#3-agrégats-entités-et-value-objects), pour ne pas dupliquer ce document.
