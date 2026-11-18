# ADR 0012 — Stockage des documents originaux et purge

- **Date :** 2026-11-17
- **Statut :** Accepted (délais de rétention et politique de doublon
  inter-documents : Proposed / Open)

## Contexte

L'original importé doit être conservé : il permet de ré-extraire (nouvelle
`ChunkingPolicy`, nouveau parseur) et de justifier les citations. L'API reçoit
le fichier, le worker d'ingestion le lit. Aucun stockage objet n'est installé
(ADR [0006](0006-technical-bootstrap.md)), et la contrainte de budget interdit
toute infrastructure sans besoin démontré.

## Décision

1. **Volume persistant local**, monté dans l'API et dans les workers, derrière
   le port `DocumentBlobStore`.
2. **Adressage par contenu** : chemin `sha256/<aa>/<bb>/<empreinte>` ; un même
   contenu n'est stocké qu'une fois.
3. **Écriture avant la transaction d'import** : stream vers un fichier
   temporaire du même volume, calcul de l'empreinte, renommage atomique ; puis
   transaction d'import. L'écriture est idempotente.
4. **Limites initiales** : 20 Mo et 300 pages par document, avec une liste
   blanche de types de média. Valeurs **à réévaluer après les mesures
   d'extraction** de S6.
5. **Pas de MinIO en V1.**
6. **Purge** :
   - retrait d'un document → purge asynchrone (work item) des points Qdrant,
     puis des chunks, puis des blobs qui ne sont plus référencés par aucune
     version ;
   - blobs orphelins (écrits sans transaction d'import aboutie) supprimés par
     la réconciliation après un délai de grâce **[Proposed : 24 h]** ;
   - les lignes document et version restent comme tombstones (traçabilité des
     citations figées par les consommateurs).
7. **Doublon inter-documents** (même empreinte importée pour deux documents
   logiques) : **[Open]**, recommandation « refuser avec 409 et l'identifiant
   existant », tranchée en S2.

## Alternatives envisagées

- **MinIO / stockage objet** : infrastructure supplémentaire sans besoin
  démontré pour un usage personnel.
- **`bytea` PostgreSQL** : transactionnel, mais alourdit la base, le WAL et les
  sauvegardes avec des fichiers jusqu'à 20 Mo.
- **Stockage par identifiant de version** : pas de dédoublonnage du contenu.

## Conséquences

- Les services `api`, `worker` et `worker_ingestion` partagent le volume
  `kb_blobs` ; les droits de l'utilisateur non-root `app` doivent être
  préparés dans l'image (S0).
- La sauvegarde du système doit inclure ce volume en plus de PostgreSQL.
- Un fichier peut exister sans ligne PostgreSQL (orphelin) ; l'inverse est
  impossible par construction (écriture avant transaction).

## Risques et compromis

- Volume local : pas de réplication ; acceptable pour un usage personnel.
- Délai de grâce : un import très lent pourrait voir son fichier temporaire
  purgé ; le délai doit dépasser largement la durée d'un upload.

## Conditions de réévaluation

- Déploiement multi-hôte ou distant (le volume local ne suffit plus).
- Mesures S6 justifiant d'autres limites.
- Besoin de sauvegarde ou de versionnement externe des originaux.

## Liens

- ADR [0007](0007-knowledge-base-bounded-context.md),
  [0010](0010-postgresql-source-of-truth-qdrant-derived-index.md),
  [0004](0004-reliable-messaging.md) (purge par work item).
- Conception : [knowledge-base.md §10](../conception/bounded-contexts/knowledge-base.md#10-publication-retrait-et-purge),
  [S0 §11](../conception/bounded-contexts/knowledge-base-s0.md#11-configuration-docker-compose).

## Suivi (2026-11-18)

Le point 7 (doublon inter-documents), **[Open]** à l'acceptation, a été
tranché pendant la slice S2 selon la recommandation : voir
[knowledge-base.md §16, O2](../conception/bounded-contexts/knowledge-base.md#16-points-ouverts).
Cette section ne modifie pas la décision ci-dessus ; elle renvoie à
l'arbitrage. Mise en œuvre : `UNIQUE(content_hash)` global sur
`knowledge_base.document_versions`, chemin `sha256/<aa>/<bb>/<empreinte>`
sous `KB_BLOB_ROOT`, temporaires sous `KB_BLOB_ROOT/tmp/` (repérables par la
réconciliation, délai de grâce toujours **[Proposed : 24 h]**), limite
`KB_MAX_UPLOAD_BYTES` (20 Mio) appliquée pendant le flux. La limite de 300
pages ne s'applique pas aux formats de S2 (Markdown, texte).

### S6 — limites mesurées (2026-11-18)

Mesures de l'extraction PDF/DOCX (point 4, « à réévaluer après les mesures
d'extraction de S6 »). Cette section ne modifie pas la décision ci-dessus.

**Où s'appliquent les limites** (arbitrage S6, [knowledge-base.md §15.1,
précisions S6](../conception/bounded-contexts/knowledge-base.md#151-composants)) :
la limite d'octets (`KB_MAX_UPLOAD_BYTES`, 20 Mio) reste appliquée à l'import
HTTP, pour tous les formats ; la limite de **300 pages** est appliquée au
**parsing** d'un PDF (constante `MAX_PDF_PAGES` du parseur), avant le pipeline
de layout : version `FAILED` `TOO_MANY_PAGES`, non rejouable. Un DOCX n'a pas
de limite de pages (pas de pagination avant mise en page).

**Machines** : hôte macOS 26.5 arm64 (Apple M5 Pro, 18 cœurs, 48 Gio), PyTorch
CPU ; conteneur `worker_ingestion` de développement (Linux 6.12 aarch64, VM
Docker Desktop de 18 vCPU et 7,75 Gio). Docling 2.150.1, OCR désactivé,
tableaux activés, une page à la fois par modèle (`layout_batch_size` et
`table_batch_size` = 1, `queue_max_size` = 4). PDF de mesure : PDF textuels A4
générés (titres, paragraphes, code, un tableau par chapitre ; 50 pages
118 918 octets, 300 pages 711 837 octets) ; DOCX équivalents (même contenu,
22 et 133 chapitres, 41 606 et 50 938 octets). Processus de mesure dédié
(parseur seul, sans modèle d'embeddings), CPU = `process_time` (tous threads).

| Mesure | Hôte | Conteneur |
|---|---|---|
| Premier PDF (2 pages, chargement des modèles depuis le cache) | 5,5 s, 6,1 s CPU | 4,5 s, 12,7 s CPU |
| PDF à chaud, 2 pages | 1,2 s (0,58 s/page), 0,83 s CPU/page | 2,0 s (1,0 s/page), 5,0 s CPU/page |
| PDF 50 pages | 33,6 s (0,67 s/page), 1,18 s CPU/page, pic RSS 2 077 Mio | 72,8 s (1,46 s/page), 8,9 s CPU/page, pic RSS 1 839 Mio |
| PDF 300 pages | 206,9 s (0,69 s/page), 1,14 s CPU/page, pic RSS 2 346 Mio | 969 s (3,2 s/page), 22,0 s CPU/page, pic RSS 2 060 Mio |
| DOCX, ~50 / ~300 pages équivalentes | 0,18 / 0,97 s (≈ 0,003 s/page) | 0,21 / 1,02 s ; RSS +24 Mio pour trois DOCX, aucun modèle |
| RSS après import des parseurs (sans conversion) | 128 Mio | 130 Mio |
| Téléchargement des poids Docling (~506 Mio, une fois) | 112,6 s | 176,7 s |

- **Réglage mesuré** (hôte, PDF de 300 pages) : avec les défauts de Docling
  (lots de 4 pages, file de 100), pic de RSS 5 815 Mio, 699 s CPU, 168 s ; file
  de 4 seule : 4 085 Mio, 690 s CPU, 168 s ; **lots d'une page et file de 4
  (retenu)** : 2 346 Mio, 343 s CPU, 207 s. Le pic de RSS ne dépend presque plus
  de la longueur (2 077 Mio à 50 pages, 2 346 à 300).
- **Conteneur** : le CPU par page y est 7 à 19 fois celui de l'hôte (noyaux
  PyTorch Linux aarch64 sous virtualisation ; `OMP_NUM_THREADS=4` ne change
  rien, `OMP_WAIT_POLICY=PASSIVE` aggrave : 16,6 s CPU/page à 50 pages), et la
  durée par page varie d'une mesure à l'autre (1,46 s/page à 50 pages et 3,2
  à 300 dans le processus de mesure, 2,0 à 300 dans le worker ci-dessous) :
  non expliqué, à surveiller.
- **Worker réel** (`worker_ingestion` de la stack de dev, PDF de 300 pages
  importé par l'API) : extraction 597 s (dont Docling 592,6 s, soit
  2,0 s/page ; 410 unités, 682 chunks), indexation 312,8 s (682 chunks
  encodés par E5, 0,46 s/chunk), synchronisation 0,02 s : **15 min 13 s** de
  l'import à `PUBLISHED`, lease prolongé par les heartbeats, une seule
  tentative. Pic de RSS du processus enfant (`VmHWM`) **2 645 Mio**, RSS
  maximale du conteneur (parent et enfant échantillonnés toutes les 2 s)
  2 871 Mio, `memory.peak` du cgroup 3 227 Mio (cache de pages compris). Un
  premier PDF de 3 pages dans ce worker : 8,6 s (chargement des modèles depuis
  le cache compris).

**Conclusion** : limites **confirmées**, 20 Mio et 300 pages.

- 300 pages : 15 min de bout en bout dans le worker ; chaque tâche reste sous
  la limite dure (`KB_WORK_TIME_LIMIT_SECONDS`, 3 600 s) : extraction 597 s
  (marge de 6), indexation 313 s ; RSS
  du worker sous 3 Gio, dans une VM de 7,75 Gio partagée avec l'API (~1,3 Gio
  après sa première recherche) et l'infrastructure. Le worker traite un item à
  la fois : un PDF de 300 pages retarde de ~15 min les imports suivants,
  acceptable pour un usage personnel. Le pic de RSS de Docling est borné (~2 Gio)
  quelle que soit la longueur ; la durée, elle, est linéaire ou pire : la
  limite reste nécessaire.
- 20 Mio : pour un PDF textuel, la limite de pages agit bien avant (300 pages
  ≈ 0,7 Mio) ; les octets d'un PDF lourd sont surtout des images, ignorées
  sans OCR. Pour un DOCX, 20 Mio de XML compressé peuvent contenir des
  centaines de fois le texte de 300 pages (51 Ko mesurés pour l'équivalent de
  300 pages) : le volume de texte d'un DOCX n'est pas borné par S6 ; point
  ouvert [O16](../conception/bounded-contexts/knowledge-base.md#16-points-ouverts).
- Les limites restent des constantes (`KB_MAX_UPLOAD_BYTES`, `MAX_PDF_PAGES`) ;
  à réévaluer sur une autre machine cible ou si l'OCR arrive (ADR 0008).

**Revue S6 — garde-fou DOCX** (2026-11-18, arbitrage de l'orchestrateur sur
[O16](../conception/bounded-contexts/knowledge-base.md#16-points-ouverts)) :
avant Docling, la somme des tailles décompressées **déclarées**
(`ZipInfo.file_size`) des parties `word/*.xml` d'un DOCX est bornée à
**100 Mio** (`MAX_DOCX_XML_BYTES`, réglable par le constructeur de
`DoclingDocxParser`) ; au-delà, version `FAILED` `DOCUMENT_TOO_COMPLEX`,
terminale (message avec la taille et la limite). Un ZIP illisible donne
`INVALID_DOCUMENT` avant Docling. La taille déclarée suffit : Docling lit
l'archive par python-docx, donc par `zipfile`, qui tronque une partie à sa
taille déclarée. Ordre de grandeur : 101 Mio de XML (des espaces) tiennent en
103 084 octets compressés, bien sous les 20 Mio de l'import ; le DOCX
équivalent à 300 pages mesuré plus haut fait 51 Ko. Ce garde-fou arrête une
bombe de décompression, pas un DOCX textuel de quelques Mio de XML : **la
borne de volume de texte commune à tous les formats (tokens extraits ou
chunks) reste ouverte pour S8** (O16). Cette section ne modifie pas la
décision ci-dessus.

**Revue S6 — mémoire sur conversions successives** (2026-11-18, M8) : un
processus de l'image de `worker_ingestion` (conteneur éphémère `docker compose
run`, stack de dev, code de la revue S6) garde **un** `DoclingPdfParser`,
comme l'enfant du worker, et convertit cinq fois le PDF de 50 pages
(118 918 octets, 407 blocs). Sans modèle d'embeddings. RSS après chaque
conversion (après `gc.collect()`) et `VmHWM` :

| Conversion | Durée | RSS après | VmHWM |
|---|---|---|---|
| (avant) | — | 128 Mio | — |
| 1 (chargement des modèles compris) | 85,0 s | 1 260 Mio | 1 786 Mio |
| 2 | 42,8 s | 1 322 Mio | 1 889 Mio |
| 3 | 45,4 s | 1 423 Mio | 1 954 Mio |
| 4 | 43,1 s | 1 398 Mio | 2 004 Mio |
| 5 | 43,9 s | 1 377 Mio | 2 038 Mio |

La RSS résiduelle se stabilise (≈ 1,4 Gio, en baisse aux conversions 4 et 5) ;
le pic, lui, croît encore à chaque conversion, par incréments décroissants
(+103, +65, +50, +34 Mio), sans plateau atteint en cinq conversions. Pas de
fuite démontrée, pas de stabilité démontrée du pic : non corrigé, à remesurer
sur davantage de conversions (et dans le worker réel, modèle d'embeddings
compris) si la mémoire du worker devient une contrainte (O5).

### S7 — purge et orphelins (2026-11-18)

Mise en œuvre du point 6 ; cette section ne modifie pas la décision ci-dessus.
Détail : [knowledge-base.md §10, précisions S7](../conception/bounded-contexts/knowledge-base.md#10-publication-retrait-et-purge)
et [README du contexte](../../backend/src/knowledge_base/README.md#s7--versions-concurrentes-retrait-et-purge).

- **Retrait** : `DELETE /api/v1/knowledge-base/documents/{id}` (202) ; une
  transaction : document `WITHDRAWN`, travail en cours annulé, item
  `purge_document`. Les lignes document et versions restent (tombstones),
  conformément au point 6.
- **Ordre de la purge** : celui du point 6, **points, puis chunks, puis
  blobs** (rétabli par la revue S7 ; la première version de S7 supprimait
  les blobs avant les chunks). Points Qdrant des index vivants hors
  transaction ; puis chunks, unités et `documents.purged_at` dans la
  transaction de finalisation du work item (fencing) ; puis, une fois cette
  transaction commitée, blobs qu'aucun document non purgé ne porte. Cette
  dernière étape est faite au mieux : un échec du volume est journalisé et
  ne fait pas échouer l'item (déjà `SUCCEEDED`) ; `purged_at` étant posée, le
  fichier n'est plus détenu et `CleanBlobOrphans` le supprime après le délai
  de grâce. Une tentative périmée (fencing refusé) ne supprime aucun blob.
  Une panne de Qdrant ne supprime rien d'autre (l'item est rejoué). L'ordre
  n'a pas d'effet sur la visibilité : le document est écarté de toute
  recherche dès le commit du retrait.
- **« Plus référencé par aucune version »** s'entend : aucune version d'un
  document **non purgé** (`purged_at IS NULL`), quel que soit son état. Les
  tombstones d'un document purgé gardent l'empreinte (un réimport reste
  refusé, O2/O11) mais ne retiennent plus le fichier.
- **Orphelins** : cas d'usage `CleanBlobOrphans` (délai de grâce
  `KB_BLOB_ORPHAN_GRACE_SECONDS`, défaut 24 h, toujours **Proposed** : rien
  n'a été mesuré) ; la date de dépôt est la date de modification du fichier.
  Son ordonnancement périodique arrive avec la réconciliation (S8) ; les
  temporaires de `tmp/` sont hors périmètre (S8). Cas couvert : un import
  refusé du contenu d'un document purgé redépose le fichier avant sa
  transaction ; ce fichier devient un orphelin ordinaire.
- **Course orphelin / import du même contenu** (revue S7 ; conséquence
  « l'inverse est impossible par construction ») : chaque import redépose le
  fichier avant sa transaction (`put` remplace le fichier par `os.replace` :
  nouvel inode, date fraîche). Une suppression sans condition entre la
  lecture des références et l'`unlink` effaçait le dépôt d'un import du même
  contenu qui venait de commiter : version sans blob, `BLOB_MISSING`
  **terminal** à l'extraction, et contenu **jamais réimportable**
  (`UNIQUE(content_hash)` global, la version en échec garde l'empreinte).
  Fermé : `CleanBlobOrphans` passe par
  `DocumentBlobEraser.delete_if_unchanged`. Le fichier est renommé
  atomiquement vers `KB_BLOB_ROOT/quarantine/` (même volume) ; il revient à
  son chemin si son inode ou sa date (ns) diffèrent de ceux de l'inventaire
  (dépôt concurrent, octets identiques), ou si les détenteurs, relus une fois
  le fichier retiré, ne sont plus vides ; sinon il est supprimé. Pas sous
  `tmp/` : le renommage garde la date de l'orphelin, plus vieille que le délai
  de grâce, et le nettoyage des temporaires de S8 pourrait l'effacer avant sa
  remise en place. Vérifié par une barrière entre la lecture des détenteurs et
  la suppression (`test_s7_blob_orphans`) : blob présent, extraction
  réussie ; avec l'ancien code, le même test perd le fichier.
  **Restent**, non mesurés : (a) une extraction qui lirait le blob d'un
  import tout juste commité pendant qu'il est hors de son chemin, entre le
  renommage et la remise en place (deux appels système, sans aller-retour
  réseau) ; (b) un import dont le dépôt a plus que le délai de grâce et dont
  la transaction ne commite qu'après la relecture des détenteurs (la borne
  que le délai de grâce doit garantir, « Risques et compromis ») ; (c) un
  crash entre la quarantaine et la remise en place, qui laisse le fichier
  sous `quarantine/` : la réconciliation S8 doit le remettre à son chemin
  s'il est détenu.
- **Schéma** : migration `9e4b7d2a6c13`, `documents.purged_at timestamptz
  NULL` (ajout de colonne sans réécriture de table).

### S8 — réconciliation du volume et mémoire du worker (2026-11-18)

Cette section ne modifie pas la décision ci-dessus. Détail :
[knowledge-base.md §10, §16 (O5, O15, O16), précisions S8](../conception/bounded-contexts/knowledge-base.md#10-publication-retrait-et-purge).

- **Point 6, orphelins** : `CleanBlobOrphans` est exécuté à chaque cycle de
  réconciliation (`KB_RECONCILE_INTERVAL_SECONDS`, défaut 300 s), délai de
  grâce `KB_BLOB_ORPHAN_GRACE_SECONDS` toujours **[Proposed : 24 h]**, non
  mesuré. Il reprend aussi les blobs d'une purge dont la suppression
  post-commit a échoué.
- **Temporaires de dépôt** (`tmp/`, suivi S2) : supprimés par
  `CleanStaleUploads` au-delà du même délai ; un temporaire récent (import en
  cours) est conservé.
- **Quarantaine** (suivi S7, point (c)) : fermé. Un fichier laissé sous
  `quarantine/` par un crash est remis à son chemin si son empreinte est
  détenue et que ce chemin est libre ; sinon supprimé une fois sa mise en
  quarantaine (`ctime`) plus vieille que le délai de grâce. Les points (a) et
  (b) du suivi S7 restent tels quels, non mesurés.
- **Purge `FAILED`** : rouverte par la réconciliation, au plus
  `KB_RECONCILE_MAX_PURGE_REOPENINGS` fois (défaut 3), puis laissée au rejeu
  explicite ; une purge réussie dont le document a de nouveau des points
  (tentative zombie) est refaite sous `purge_document:<id>:<n>`.
- **Volume de texte (point 4, O16)** : borne commune à tous les formats,
  `KB_MAX_DOCUMENT_TOKENS` = 1 000 000 tokens d'unités (Proposed) ; au-delà,
  `DOCUMENT_TOO_LARGE`, terminal et non rejouable. Origine du défaut : 3,74
  caractères par token mesurés avec le tokenizer E5 sur les fixtures FR/EN
  (15 687 caractères) ; un PDF très dense de 300 pages (~5 000 caractères par
  page) fait ~400 000 tokens, celui de S6 (410 unités, 682 chunks d'au plus
  508 tokens) au plus 350 000. Les limites de 20 Mio et 300 pages restent.
- **Mémoire sur conversions successives** (revue S6, M8) : décision sans
  nouvelle mesure longue (orchestrateur) : `worker_ingestion` remplace son
  processus enfant toutes les `KB_INGESTION_MAX_TASKS_PER_CHILD` tâches
  (défaut 50, `--max-tasks-per-child`), ce qui borne la croissance du pic
  mesurée (+103, +65, +50, +34 Mio sur cinq conversions) au prix d'un
  rechargement des modèles par nouvel enfant (E5 ~4 s, Docling PDF ~5 s,
  mesures S5/S6). Vérifié sur la stack de dev le 2026-11-18 : `celery inspect
  stats` rend `"max-tasks-per-child": 50`. Aucun redémarrage d'enfant n'a été
  observé ni mesuré (moins de 50 tâches pendant la vérification).
