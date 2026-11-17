# ADR 0007 — Introduction du Bounded Context Knowledge Base

- **Date :** 2026-11-17
- **Statut :** Accepted

## Contexte

L'ADR 0001 sépare Interview Training et Assessment. Knowledge Base figurait
parmi les contextes « envisagés » ([001](../conception/001-bounded-contexts.md)).
L'application doit importer des documents pédagogiques, les versionner, les
indexer et fournir des passages sourcés aux deux contextes existants, sans
les coupler à Qdrant ni aux tables d'ingestion. La revue de conception du
2026-11-17 ([knowledge-base.md](../conception/bounded-contexts/knowledge-base.md))
a validé ce troisième contexte.

## Décision

1. **Knowledge Base est un Bounded Context** à part entière, sous
   `backend/src/knowledge_base/`. Il possède l'import, l'original, les
   versions, l'extraction, les unités de connaissance, le chunking,
   l'indexation, la recherche sémantique, le retrait et la purge.
2. **Relation upstream / Open Host Service.** Interview Training et Assessment
   n'y accèdent que par ses contrats publics, d'abord
   `knowledge_base.contracts.providers.KnowledgeSearch` (query synchrone en
   processus), dont l'implémentation leur est injectée par `runtime`
   ([005](../conception/005-project-structure.md#surface-publique-dun-contexte-et-composition-root)).
3. **Aucun accès** aux tables `knowledge_base.*`, aux collections Qdrant ou
   aux fichiers originaux depuis un autre contexte.
4. **Les consommateurs figent** les extraits et citations qu'ils utilisent
   dans leurs propres snapshots.
5. **Aucun Integration Event publié en V1.** Les noms
   `knowledge.document_version_published.v1` et
   `knowledge.document_withdrawn.v1` sont réservés ; ils seront définis selon
   [004](../conception/004-integration-contracts.md) quand un consommateur réel
   existera.
6. **Langage :** `KnowledgeDocument` (document logique) et `DocumentSource`
   (provenance) remplacent le terme `KnowledgeSource` de la première version
   de [domain.md](../context/domain.md).

## Alternatives envisagées

- **Module technique RAG sans Bounded Context** : simple au départ, mais le
  cycle de vie des documents (versions, retraits, provenance) porte de vrais
  invariants métier.
- **Recherche intégrée à Assessment** : couplerait la correction à l'ingestion
  et empêcherait Interview Training de réutiliser les sources.
- **Accès direct des consommateurs à Qdrant** : rejeté ; il rendrait
  impossibles le contrôle de visibilité, la migration d'index et l'évolution
  du schéma de payload.

## Conséquences

- Un troisième contexte apparaît dans les tests d'architecture
  (`test_src_contains_the_validated_contexts`), mis à jour avec le premier code
  (S0).
- Le composition root doit pouvoir câbler des workers Celery et fournir un
  contrat à d'autres contextes : la surface publique d'un contexte est élargie
  dans 005.
- Les contextes consommateurs dépendent d'un contrat synchrone ; une
  indisponibilité de Knowledge Base dégrade leurs fonctions de
  contextualisation, sans bloquer leurs invariants propres.

## Risques et compromis

- Appel synchrone en processus : simple et sans réseau, mais couplage
  temporel. Acceptable dans un monolithe modulaire ; un passage à un appel
  distant ne changerait que l'adapter.
- Snapshot côté consommateur : duplication volontaire de texte, au profit de la
  stabilité historique des entretiens et corrections.

## Conditions de réévaluation

- Apparition d'un consommateur qui doit réagir à une publication ou à un
  retrait (Question Bank, cache côté Assessment) : définir et publier les
  Integration Events réservés, via outbox (ADR 0004).
- Besoin multi-utilisateur ou de documents privés : contraintes d'accès dans
  `KnowledgeQuery`.

## Liens

- ADR [0001](0001-two-bounded-contexts.md) (inchangé, complété par celui-ci),
  [0002](0002-event-choreography-v1.md), [0003](0003-modular-monolith-clean-architecture.md),
  [0004](0004-reliable-messaging.md).
- ADR [0008](0008-custom-pipeline-docling-extraction.md) à
  [0012](0012-original-document-storage-and-purge.md).
- Conception : [knowledge-base.md](../conception/bounded-contexts/knowledge-base.md).
