# 001 — Bounded Contexts et Context Map

## Décisions confirmées

- **Interview Training** et **Assessment** sont séparés (ADR 0001).
- Communication inter-contextes principale par **chorégraphie événementielle** (ADR 0002).
- **Knowledge Base** est le troisième contexte (ADR 0007).

## Interview Training

Propriétaire des séances, politiques de parcours, questions présentées, soumissions, navigation, et visibilité du feedback. Ne calcule jamais les scores.

## Assessment

Propriétaire des demandes d'évaluation, barèmes, critères, notes, feedback, révisions et traçabilité des tentatives. Ne fait jamais avancer une séance.

## Knowledge Base

Propriétaire de l'import des documents pédagogiques, de leurs versions, de l'extraction, des unités de connaissance, du chunking, de l'indexation et de la recherche sémantique sourcée. Ne déroule pas d'entretien et ne note pas. Conception : [knowledge-base.md](bounded-contexts/knowledge-base.md).

## Relations

- Interview Training est **upstream** pour les faits de soumission.
- Assessment est **upstream** pour les résultats publiés.
- Knowledge Base est **upstream** (Open Host Service) pour la recherche : Interview Training et Assessment consomment le contrat public synchrone `KnowledgeSearch` et figent les extraits utilisés. Aucun Integration Event Knowledge Base en V1.
- Chaque contexte maintient ses propres entités, repositories, tables et DTO.
- Les événements externes sont adaptés en commandes locales via une couche anti-corruption si nécessaire.
- Les données ORM et les agrégats ne traversent jamais les frontières.

## Contextes envisagés

- **Question Bank** : catalogue de questions, provenance et validation, si modèle métier autonome.
- **Learning Progress** : compétences, lacunes et révisions espacées.
- **LLM Gateway** : composant technique derrière un port, pas un BC par défaut.

Un dépôt Git et une base PostgreSQL peuvent être partagés, mais il n'existe pas de transaction métier inter-contextes. Les échanges sont à cohérence éventuelle.
