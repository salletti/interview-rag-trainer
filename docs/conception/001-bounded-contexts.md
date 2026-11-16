# 001 — Bounded Contexts et Context Map

## Décisions confirmées

- **Interview Training** et **Assessment** sont séparés (ADR 0001).
- Communication inter-contextes principale par **chorégraphie événementielle** (ADR 0002).

## Interview Training

Propriétaire des séances, politiques de parcours, questions présentées, soumissions, navigation, et visibilité du feedback. Ne calcule jamais les scores.

## Assessment

Propriétaire des demandes d'évaluation, barèmes, critères, notes, feedback, révisions et traçabilité des tentatives. Ne fait jamais avancer une séance.

## Relations

- Interview Training est **upstream** pour les faits de soumission.
- Assessment est **upstream** pour les résultats publiés.
- Chaque contexte maintient ses propres entités, repositories, tables et DTO.
- Les événements externes sont adaptés en commandes locales via une couche anti-corruption si nécessaire.
- Les données ORM et les agrégats ne traversent jamais les frontières.

## Contextes envisagés

- **Knowledge Base** : ingestion, version des documents, indexation RAG.
- **Question Bank** : catalogue de questions, provenance et validation, si modèle métier autonome.
- **Learning Progress** : compétences, lacunes et révisions espacées.
- **LLM Gateway** : composant technique derrière un port, pas un BC par défaut.

Un dépôt Git et une base PostgreSQL peuvent être partagés, mais il n'existe pas de transaction métier inter-contextes. Les échanges sont à cohérence éventuelle.
