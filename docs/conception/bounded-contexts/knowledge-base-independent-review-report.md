# Knowledge Base — rapport de la review indépendante (Codex)

> Review effectuée par Codex, lancée par l'utilisateur hors de cet
> environnement, sur `feature/knowledge-base-v1` au commit `28e29fc`, comparé
> à `main` (`447a234`). Rapport transmis le 2026-11-23 et reproduit ci-dessous
> sans modification de fond (les liens absolus vers les fichiers sont
> remplacés par des chemins du dépôt). Dossier de lancement :
> [knowledge-base-independent-review.md](knowledge-base-independent-review.md).
> Les corrections sont suivies dans [le suivi](knowledge-base-progress.md).

Quatre constats : un bloquant et trois importants. Aucun fichier source
modifié par la review.

## 1. Deux versions du même document peuvent apparaître dans une seule réponse

- **Fichier** : `backend/src/knowledge_base/use_cases/search_knowledge/search_knowledge_handler.py:120`
- **Sévérité** : bloquant
- **Défaut** : chaque passe contrôle uniquement ses nouveaux résultats SQL.
  Les unités conservées lors des passes précédentes ne sont jamais
  revérifiées. Une publication entre deux passes permet donc de conserver une
  unité de N, puis d'ajouter une unité de N+1.
- **Impact** : contexte RAG contradictoire, avec deux versions d'un même
  document dans la réponse. Cela contredit la garantie de non-mélange de la
  conception (§8 et §10) : les contrôles SQL successifs ne constituent pas un
  instantané cohérent du résultat.
- **Reproduction** : avec `limit=2`, première passe de quatre unités, dont une
  de N acceptée et trois rejetées ; publier et synchroniser N+1 avant le
  complément ; celui-ci retrouve une unité de N+1. Reproduction exécutée sur
  le vrai handler, avec des doubles respectant les contrats : versions 3 et 4
  du même document renvoyées ensemble.
- **Correction suggérée** : effectuer un contrôle SQL commun de tous les
  candidats avant de construire la réponse ; gérer les rejets supplémentaires
  dans le complément borné et dans `complete`. Ajouter un test avec barrière
  entre les passes : le test S7 actuel ne force pas cet entrelacement.

## 2. Une question longue contourne le budget du passage

- **Fichier** : `backend/src/knowledge_base/domain/services/passage_assembler.py:75`
- **Sévérité** : important
- **Défaut** : le chemin tronqué choisit sa fenêtre selon les tailles des
  chunks, puis préfixe le corps avec la question intégrale. Or le chunking a
  pu tronquer cette question dans son en-tête.
- **Impact** : `max_passage_tokens` ne borne plus le texte rendu,
  contrairement à l'ADR 0009, point 6. Une question volumineuse peut saturer
  le contexte du consommateur.
- **Reproduction** : segmentation, chunking et assemblage réels avec le
  compteur de mots des tests : question de 5 001 tokens, chunk de 255 tokens,
  budget de passage 1 024, résultat de 5 004 tokens, malgré `truncated=true`.
- **Correction suggérée** : réserver le budget de la question avant la
  fenêtre, traiter explicitement une question dépassant seule ce budget et
  contrôler le texte final. Ajouter ce cas aux tests de reconstruction.

## 3. Sans Content-Length, le multipart reste entièrement consommé avant le refus

- **Fichier** : `backend/src/knowledge_base/adapters/controller/rest/documents_router.py:231`
- **Sévérité** : important
- **Défaut** : lorsque l'en-tête est absent, la route délègue à FastAPI, qui
  parse le formulaire et stocke le fichier temporaire avant que `_chunks()` et
  le blob store appliquent leur limite.
- **Impact** : un upload HTTP chunked peut consommer une quantité non bornée
  de disque temporaire et de bande passante avant le 413. Le correctif M-1
  protège uniquement la taille déclarée.
- **Reproduction** : requête ASGI diffusée par morceaux, sans
  `Content-Length` : limite configurée à 1 000 octets, plafond avec marge à
  66 536, mais 200 455 octets consommés intégralement avant le 413.
- **Correction suggérée** : compter les octets reçus avant le parsing
  multipart et interrompre au dépassement du plafond global ; conserver le
  contrôle exact du fichier. Le test existant vérifie le statut 413, mais pas
  l'arrêt de lecture.

## 4. Le garde-fou DOCX ignore des parties effectivement décompressées

- **Fichier** : `backend/src/knowledge_base/adapters/gateway/parsing/docling_docx_parser.py:64`
- **Sévérité** : important
- **Défaut** : le contrôle additionne seulement les entrées `word/*.xml`. Le
  chargeur python-docx utilisé par Docling charge aussi les parties
  accessibles par les relations OPC, notamment les parties binaires
  incorporées.
- **Impact** : une archive très compressible peut dépasser largement la
  mémoire prévue sans déclencher `DOCUMENT_TOO_COMPLEX`. Cela aggrave O16
  au-delà de la limite connue du volume de texte.
- **Reproduction** : DOCX valide enrichi d'une relation vers
  `word/embeddings/data.bin` : archive de 39 756 octets, contrôle accepté avec
  une limite XML de 1 Mio, puis chargement effectif des 2 Mio de la partie
  binaire par `PackageReader`. La même exclusion s'applique à la limite de
  production de 100 Mio.
- **Correction suggérée** : borner la taille décompressée totale des parties
  susceptibles d'être chargées, idéalement de toute l'archive, avant Docling.
  Ajouter un test avec partie binaire reliée dépassant cette limite.

## Validations exécutées par la review

- `make lint typecheck test` : réussi ; 1 759 tests passent.
- `make test-integration` : réussi ; 548 tests passent, avec 31 avertissements
  de dépréciation.
- Stack `compose.test.yaml` arrêtée et supprimée après exécution. Aucun accès
  à la stack de développement.
- Aucun écart supplémentaire confirmé sur les chemins de fencing, les
  migrations ou les frontières examinés. L'ADR 0013 et la barrière de
  l'ADR 0011 restent traités comme Proposed.

## Non vérifié par la review

- La course du constat 1 avec une barrière sur PostgreSQL/Qdrant réels :
  reproduction déterministe réalisée avec les doubles des ports.
- L'épuisement effectif de mémoire par un DOCX massif : vérification limitée
  au contournement du contrôle et au chargement réel des parties.
- Les coupures physiques, tous les entrelacements concurrents possibles et les
  performances sur un grand corpus.

## Problème / Avant / Après (tel que rendu par la review)

La colonne « Après » décrit les corrections attendues, non appliquées au
moment de la review.

| Problème | Avant — faits établis | Après — attendu |
|---|---|---|
| Versions mélangées | Le handler reproduit renvoie N et N+1 ensemble | Contrôle cohérent de tous les candidats |
| Budget de passage | 5 004 tokens rendus pour un budget de 1 024 | Question et corps respectent le budget |
| Upload chunked | Corps entier consommé avant le 413 | Lecture interrompue au plafond global |
| Décompression DOCX | Partie binaire chargée hors du comptage XML | Volume décompressé total borné |

## Seconde passe (commit `0d8d063`)

Seconde review effectuée par Codex, lancée par l'utilisateur, sur les
corrections de `0d8d063`. Elle a été transmise le 2026-11-23 et son contenu
est reproduit sans modification de fond.

**Les quatre constats sont corrigés.** Aucun nouveau défaut n'a été identifié
dans les corrections examinées, et le bloquant est levé.

### Vérifications relancées indépendamment

- Ruff et mypy : aucune erreur.
- 1 780 tests unitaires et d'architecture passent.
- 549 tests d'intégration passent, dont la publication forcée entre deux
  passes sur PostgreSQL et Qdrant réels.
- Les tests de régression échouent bien quand chacun des anciens
  comportements est réintroduit en mémoire, sans modifier les fichiers.
- Avec le tokenizer E5 réel, la question longue testée produit un passage de
  762 tokens, sous le plafond de 1 024.

Aucun fichier source n'a été modifié et la stack de développement n'a pas été
utilisée. La stack de test a été supprimée après l'exécution.

### Non vérifié

La validation porte sur les correctifs de `0d8d063`. Trois points restent non
vérifiés :

- la mémoire de Docling à l'approche de 100 Mio décompressés ;
- le coût des archives qui comportent un très grand nombre d'entrées ;
- l'effet mémoire du recyclage des workers.

Le champ `question` reste intégral ; seul `text` est borné. Les décisions
encore Proposed restent à arbitrer.

### Problème / Avant / Après

| Problème | Avant — faits établis | Après — vérifié |
|---|---|---|
| Versions mélangées | N et N+1 renvoyées ensemble | Revalidation commune ; test concurrent réel réussi |
| Passage trop long | 5 403 tokens pour un budget de 1 024 | Budget respecté ; vérification avec E5 réel |
| Upload chunked | 200 455 octets lus avant le refus | Lecture interrompue au plafond, à un message ASGI près |
| Décompression DOCX | Partie binaire hors du comptage XML | Toutes les entrées comptées ; dépassement refusé avant Docling |
