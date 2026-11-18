# ADR 0009 — Chunking pédagogique par KnowledgeUnit

- **Date :** 2026-11-17
- **Statut :** Accepted (paramètres numériques et heuristiques : Proposed)

## Contexte

Les documents contiennent des cours, des fiches de révision, des questions
d'entretien avec réponses et des exemples de code Python. Un découpage
générique (caractères, tokens fixes, similarité sémantique) sépare une
question de sa réponse ou coupe un exemple de code. La recherche doit
retrouver ensemble la question, la réponse de référence, le code et
l'explication, et citer précisément sa source.

## Décision

1. **Unité de connaissance (`KnowledgeUnit`)** construite par un service de
   domaine (`KnowledgeUnitSegmenter`) à partir du `StructuredDocument`
   (ADR 0008), selon le `DocumentKind` :
   - `QA_SHEET` : question + réponse + exemples de code + explications ;
   - `COURSE`, `TECHNICAL` : une section de titre (sous-sections fusionnées
     tant que le budget le permet) ;
   - `REVISION_SHEET` : une définition ou une fiche.
2. **Conventions explicites prioritaires** (front-matter `kind: qa_sheet`,
   titres structurés) sur les heuristiques de détection.
3. **`ChunkPlanner`** : une unité qui tient dans le budget en tokens du profil
   d'embeddings donne un chunk ; au-delà, elle est découpée **aux frontières
   de blocs**, et chaque fragment reprend l'en-tête contextuel (titre du
   document, chemin de sections, question racine). Le comptage utilise le
   tokenizer du profil (port `TokenCounter` fourni par l'`Embedder`).
4. **Fallback documenté** pour un bloc de code ou un tableau plus grand que le
   budget : découpe aux lignes vides ou aux définitions (`def`, `class`), puis
   aux lignes en dernier recours ; chaque fragment porte l'en-tête et un
   marqueur de continuation. Il n'y a **pas** de promesse « jamais coupé ».
5. **Overlap configurable**, valeur initiale 0 ; ce n'est pas un invariant
   métier. Sa valeur est mesurée en S5/S9.
6. **Small-to-big** : la recherche reconstruit l'unité complète depuis
   PostgreSQL, dans une limite explicite `max_passage_tokens` ; au-delà, une
   fenêtre contenant la question racine et les chunks trouvés, signalée par
   `truncated=true`.
7. **`ChunkingPolicy` versionnée** : règles, budget, overlap et fallbacks ont
   une version qui fait partie de l'identité des unités et chunks produits.
   Une politique appartient à un `SearchIndex` (ADR 0011).
8. **Texte embarqué** = en-tête contextuel + contenu ; le préfixe propre au
   modèle (`passage: ` pour E5) est ajouté par l'adapter d'embeddings, pas
   stocké.

## Alternatives envisagées

- **Recursive Character Splitter, découpe par tokens fixes** : ignorent la
  structure et coupent le code.
- **Semantic chunking** : un embedding par phrase, frontières instables,
  inadapté au code.
- **Docling HybridChunker seul** : respecte la structure et le tokenizer, mais
  ignore la relation question/réponse ; il peut servir d'outil interne pour
  les découpes de fallback.
- **Overlap par défaut** : doublons dans les résultats et citations moins
  nettes ; remplacé par l'en-tête contextuel, à remesurer.

## Conséquences

- Les règles de segmentation sont du code de domaine pur, testé par golden
  tests (S3).
- La qualité dépend de la structure des documents : les fiches personnelles
  suivent une convention explicite.
- Un changement de profil à longueur maximale plus courte peut exiger un
  nouveau lot de chunks (ADR 0011).

## Risques et compromis

- Heuristiques Q/R fragiles sur des documents tiers : mesurées en S3/S5.
- Unités longues : la reconstruction est bornée, au prix d'une troncature
  signalée.
- Budget de 512 tokens pour E5 : les unités avec beaucoup de code seront plus
  souvent fragmentées ; argument à peser dans le benchmark S9.

## Conditions de réévaluation

- Métriques S5 (intégrité d'unité, Recall@5) insuffisantes.
- Changement de profil d'embeddings (S9).
- Nouveaux types de documents.

## Liens

- ADR [0008](0008-custom-pipeline-docling-extraction.md),
  [0010](0010-postgresql-source-of-truth-qdrant-derived-index.md),
  [0011](0011-embedding-profile-search-index-blue-green.md).
- Conception : [knowledge-base.md §15.2](../conception/bounded-contexts/knowledge-base.md#152-chunking-pédagogique-résumé--détail-dans-ladr-0009).

## Suivi (2026-11-18)

Précision d'implémentation (S3), sans changement de décision : l'identité de
la `ChunkingPolicy` (point 7) comprend le **tokenizer** utilisé pour compter le
budget, puisque le budget effectif en dépend : `identity = version+tokenizer`
(production : `kb-chunking-v1+approx-xlmr-v1`, compteur provisoire majorant).
Le passage au tokenizer réel du profil (S4) produit donc une nouvelle identité
de lot. Revue du commit S3 : voir
[suivi Knowledge Base](../conception/bounded-contexts/knowledge-base-progress.md).

S4 (2026-11-18) : le compteur de production est le tokenizer du profil
(`SentenceTransformersEmbedder.token_counter`) ; identité de production
`kb-chunking-v1+xlmr-d1287505`, budget inchangé (508). Les lots de l'identité
provisoire ne sont pas réindexés (pas de backfill avant S9).

S5 (2026-11-18) : small-to-big (point 6) implémenté
(`domain/services/passage_assembler.py`) ; le budget d'une unité complète
compte la question ; une fenêtre ne répète pas l'en-tête contextuel des
chunks (précisions au [§12](../conception/bounded-contexts/knowledge-base.md#12-stratégie-de-recherche-et-limites-de-recall)).
Première mesure des conditions de réévaluation : intégrité d'unité 1,000
(37 unités retrouvées sur 37) et Recall@5 0,881 sur le jeu de référence
(42 requêtes) ; seuils de non-régression dans
`backend/tests/fixtures/knowledge_base/retrieval/retrieval_thresholds.json`.
Le jeu n'exerce ni les unités multi-chunks ni la troncature (49 unités, toutes
en un chunk) : `max_passage_tokens` et l'overlap (point 5) restent à mesurer
sur des cours longs (S6, S9).

Revue S5 (2026-11-18), requalification sans changement de décision :
l'intégrité d'unité 1,000 est **non exercée** en S5. Avec des unités d'un seul
chunk, retrouver l'unité suffit à rendre l'unité entière : la mesure ne peut
pas échouer et ne dit rien du point 6 (small-to-big). Le seuil (0,94) est
conservé ; la condition de réévaluation qui en dépend ne sera significative
qu'avec les unités multi-chunks de S6. Le budget compte désormais la question
avec le tokenizer du profil, dans le domaine (`passage_assembler`), et la
fenêtre retire aussi un en-tête contextuel tronqué par le `ChunkPlanner`.
