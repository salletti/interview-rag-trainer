# Contexte métier — Interview RAG Trainer

## Objectif produit

Application personnelle d'entraînement à des entretiens techniques en Python, FastAPI, Celery, DDD, RAG et Agentic AI. Poser une question à la fois, recueillir une réponse écrite (puis orale), corriger à l'aide d'une grille explicite et mémoriser les lacunes. La récupération de connaissances (RAG) aide à contextualiser les questions et à justifier les corrections.

## Cas d'usage

1. Démarrer une séance en mode entretien interactif ou examen blanc.
2. Présenter une question issue d'un catalogue ou générée à partir de sources.
3. Soumettre une réponse et recevoir un accusé de réception immédiat.
4. Évaluer la réponse selon un barème versionné et publier le feedback.
5. Afficher le feedback immédiatement ou en fin de séance.
6. Conserver compétences, erreurs et révisions espacées (futur Learning Progress).
7. Utiliser des fournisseurs LLM gratuits avec fallback sans modifier le domaine.

## Ubiquitous Language

| Terme | Signification |
|---|---|
| TrainingSession | Séance et règles de déroulement |
| InterviewTurn | Tour numéroté présentant une question |
| QuestionSnapshot | Texte, consignes, provenance et version figés |
| Submission | Réponse finale enregistrée pour un tour |
| SessionPolicy | Règles de parcours et de visibilité du feedback |
| Assessment | Évaluation logique d'une soumission |
| Rubric | Barème versionné avec critères |
| CriterionResult | Résultat pour un critère d'évaluation |
| AssessmentAttempt | Tentative technique de correction |
| AssessmentRevision | Résultat publié immuable |
| Feedback | Correction et justification pédagogique |
| LearningGap | Lacune suivie à travers des séances |
| KnowledgeSource | Document de référence du système RAG |
| IntegrationEvent | Fait publié entre Bounded Contexts |

## Principes métier

Interview Training possède le parcours, jamais la note. Assessment possède la correction, jamais la navigation. La correction n'est pas une simple réponse brute d'un LLM : le système applique des validations déterministes. Scores et historique doivent rester traçables et comparables.

## Périmètre V1

Deux contextes confirmés : Interview Training et Assessment. Knowledge Base, Question Bank et Learning Progress restent candidats à formaliser plus tard. Le routeur LLM est une capacité technique, pas nécessairement un Bounded Context.
