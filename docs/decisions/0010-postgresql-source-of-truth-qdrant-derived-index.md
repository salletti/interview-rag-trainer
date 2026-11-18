# ADR 0010 — PostgreSQL source de vérité, Qdrant index dérivé

- **Date :** 2026-11-17
- **Statut :** Accepted (paramètres de recherche : Proposed jusqu'à S5)

## Contexte

Qdrant est le moteur vectoriel (ADR [0006](0006-technical-bootstrap.md),
point 3). Une écriture Qdrant et une transaction PostgreSQL ne sont pas
atomiques. Il faut pourtant garantir qu'une version non publiée, supplantée ou
retirée n'apparaît jamais dans une recherche, citer les sources de façon
fiable, réindexer sous un autre modèle sans re-parser, et vérifier l'intégrité
de l'index au-delà d'un simple comptage.

## Décision

1. **PostgreSQL est la source de vérité** des documents, versions, unités,
   chunks (texte, empreinte, citation) et de la visibilité. **Qdrant est un
   index dérivé et reconstructible** : vecteurs et payload de filtrage.
2. **Identifiants déterministes** : `chunk_id` dérivé de la version, de la
   `ChunkingPolicy` et des positions ; point Qdrant `uuid5(index_id, chunk_id)`.
   Les upserts sont des écrasements idempotents.
3. **Manifest vérifié** : une `Indexation` ne passe `COMPLETED` qu'après
   upserts confirmés (`wait=true`), relecture des points par identifiants et
   égalité exacte des couples `(point_id, chunk_hash)` avec le manifest
   attendu ; l'empreinte du manifest est conservée. La configuration de la
   collection (dimension, distance) est vérifiée. Un `count` seul ne suffit pas.
4. **Visibilité arbitrée par PostgreSQL** : visible si et seulement si la
   version est `PUBLISHED` et le document `ACTIVE`.
5. **Drapeau `visible` synchronisé dans Qdrant**, après le commit, par un
   travail convergent (`sync_visibility`) : il filtre avant le top-k pour
   préserver le Recall ; il ne décide jamais.
6. **Vérification SQL finale** de chaque recherche, puis **une passe de
   complément** si des passages sont écartés ; le résultat signale
   `complete=false` s'il reste incomplet.
7. **Points écrits `visible=false`**, puis publication SQL, puis
   synchronisation. Pendant la fenêtre de synchronisation, un document
   republié peut manquer temporairement ; il ne peut jamais apparaître à tort.
8. **Retrait** : invisibilité garantie pour toute recherche **commencée après**
   le commit ; purge physique asynchrone.
9. **Aucun effet Qdrant n'est garanti « exactly once »** : les écritures sont
   idempotentes et récupérables ; la réconciliation corrige les écarts (points
   en trop, orphelins, manifests divergents).

## Alternatives envisagées

- **Qdrant seul** (texte et métadonnées dans le payload) : pas de citation ni
  de réindexation fiables, pas de transaction pour la visibilité.
- **Filtrage SQL seul après Qdrant** : simple, mais perte de Recall quand de
  nombreux points obsolètes occupent le top-k.
- **Drapeau Qdrant seul** : non atomique avec PostgreSQL, donc des résultats
  obsolètes possibles.
- **pgvector** : écarté par l'ADR 0006 ; cet ADR ne rouvre pas ce choix.

## Conséquences

- Duplication contrôlée du texte (PostgreSQL) et des vecteurs (Qdrant).
- Chaque recherche fait au moins une requête Qdrant et une requête SQL.
- Des index de payload sont nécessaires (`visible`, `unit_id`, `document_id`,
  `topics`, `kind`, `language`).
- La réconciliation devient une fonction de premier plan (S8).

## Risques et compromis

- Perte de Recall transitoire pendant la synchronisation, mesurée en S5/S7.
- Coût de la vérification du manifest (relecture de tous les points d'une
  version) : acceptable au volume visé, à remesurer.
- Tentative zombie réécrivant des points après une purge : points non visibles,
  supprimés par la réconciliation.

## Conditions de réévaluation

- Recall mesuré en S5 insuffisant malgré le complément.
- Volume rendant la vérification exhaustive du manifest trop coûteuse
  (passage à l'échantillonnage).
- Besoin de recherche hybride (sparse) imposant un autre schéma de collection.

## Liens

- ADR [0004](0004-reliable-messaging.md) (work queue), [0006](0006-technical-bootstrap.md),
  [0009](0009-pedagogical-chunking-knowledge-units.md),
  [0011](0011-embedding-profile-search-index-blue-green.md).
- Conception : [knowledge-base.md §9–§12](../conception/bounded-contexts/knowledge-base.md#9-intégrité-de-lindex-qdrant).

## Suivi (2026-11-18)

Implémentation de S5, le corps de l'ADR est inchangé. Les points 5 et 6 sont
en place : `visible=true` est ajouté à chaque recherche par l'adapter Qdrant ;
le contrôle SQL final (`SqlAlchemyPublishedUnitReader`) ne garde que les
unités de versions `PUBLISHED` de documents `ACTIVE` ; une passe de complément
par défaut, `complete=false` quand des rejets laissent moins de `limit`
passages. Les « paramètres de recherche Proposed jusqu'à S5 » sont tranchés
dans [knowledge-base.md §16, O12](../conception/bounded-contexts/knowledge-base.md#16-points-ouverts)
(f = 2, une passe ; `max_passage_tokens` reste Proposed) et précisés au §12.
Mesures sur le jeu de référence S5 : Recall@5 0,881, MRR 0,853, intégrité
d'unité 1,000 ; recherche complète en 20 ms médians sur l'hôte (encodage
13 ms, Qdrant 4 ms, contrôle SQL 2 ms). La condition de réévaluation
« Recall mesuré en S5 insuffisant malgré le complément » n'est pas atteinte :
avec 3 documents sur 10 écartés par le contrôle SQL, Recall@5 reste à 0,917
dès f = 2. Détail : [README du contexte](../../backend/src/knowledge_base/README.md#s5--recherche-sémantique-provenance-et-évaluation).

### S7 (2026-11-18)

Le corps de l'ADR est inchangé. Point 8 vérifié sous concurrence réelle
(`test_s7_withdrawal_purge`) : après le commit d'un retrait, aucune recherche
ne rend le document, alors que Qdrant porte encore ses points visibles (le
retrait ne planifie pas `sync_visibility`, la purge supprime les points). Le
risque « tentative zombie réécrivant des points après une purge » est
observé : points `visible=false`, jamais renvoyés ; leur suppression attend
une nouvelle exécution de `purge_document`, que la réconciliation S8
planifiera. Point 7 vérifié pendant une republication : aucun résultat ne mêle
deux versions d'un document, y compris quand les deux sont visibles dans
Qdrant. Défaut corrigé : un hit au payload incomplet (point supprimé pendant
la requête) faisait échouer la recherche ; il est ignoré. Détail :
[knowledge-base.md §8, §10, précisions S7](../conception/bounded-contexts/knowledge-base.md#10-publication-retrait-et-purge).

### S8 (2026-11-18)

Le corps de l'ADR est inchangé. Point 9 (« la réconciliation corrige les
écarts ») mis en œuvre par `ReconcileKnowledgeBase`, un cycle par créneau
([knowledge-base.md §9, §11, précisions S8](../conception/bounded-contexts/knowledge-base.md#9-intégrité-de-lindex-qdrant)) :
Qdrant est lu **avant** PostgreSQL (parcours complet des index vivants, sans
vecteur), si bien qu'un point écrit pendant le cycle n'est jamais pris pour
un orphelin. Écarts corrigés et vérifiés contre Qdrant réel : points sans
propriétaire ou de versions `SUPERSEDED`/`DISCARDED` (supprimés), points
invisibles d'une version publiée (`sync_visibility` replanifiée : la fenêtre
résiduelle de la tentative zombie de S4 est refermée), points écrits après
une purge (`purge_document` replanifiée), manifests échantillonnés (au plus
`KB_RECONCILE_MANIFEST_SAMPLE_SIZE` par cycle ; écart → indexation rouverte et
refaite), points hors manifest d'une version vérifiée (supprimés par
identifiant). La condition de réévaluation « volume rendant la vérification
exhaustive du manifest trop coûteuse » est anticipée par l'échantillonnage ;
le parcours complet des points à chaque cycle reste à mesurer sur un grand
corpus.
