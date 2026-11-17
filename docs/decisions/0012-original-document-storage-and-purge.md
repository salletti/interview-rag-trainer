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
