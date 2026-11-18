# ADR 0008 — Pipeline d'ingestion personnalisé et extraction Docling

- **Date :** 2026-11-17
- **Statut :** Accepted (détails d'installation : Proposed, voir « Décision », point 6)

## Contexte

Knowledge Base (ADR 0007) doit extraire PDF, DOCX, Markdown et TXT en
préservant titres, tableaux, pages, blocs de code et relations entre
questions et réponses, sur CPU, sans service payant. L'audit du 2026-11-17 a
comparé Docling, Unstructured, PyMuPDF/pymupdf4llm et MarkItDown pour
l'extraction, puis un pipeline personnalisé, LlamaIndex `IngestionPipeline` et
LangChain pour l'orchestration. Qdrant est déjà le moteur vectoriel retenu par
l'ADR [0006](0006-technical-bootstrap.md) (point 3) ; Celery et RabbitMQ y sont
installés (point 6).

## Décision

1. **Pipeline personnalisé**, sans LangChain ni LlamaIndex en V1. Les états,
   retries, versions et provenance appartiennent au domaine et à PostgreSQL.
2. **Docling** est l'extracteur pour PDF, DOCX et Markdown. Sa sortie
   (`DoclingDocument`) est traduite par l'adapter en `StructuredDocument`,
   représentation neutre du domaine (couche anti-corruption) ; aucun type
   Docling ne sort de `adapters/gateway/parsing/`.
3. **TXT** est traité par un adapter natif (paragraphes et blocs simples),
   sans dépendre de la prise en charge de ce format par Docling.
4. **SentenceTransformers** calcule les embeddings localement ; **Qdrant
   Client** écrit et interroge l'index. Chaque bibliothèque est confinée à son
   adapter derrière un port : `DocumentParser`, `Embedder`, `VectorIndex`.
5. **OCR désactivé en V1.** Un PDF sans couche texte échoue avec le code
   terminal `OCR_REQUIRED`.
6. **Installation [Proposed, confirmée au lock de S3/S4/S6]** :
   `docling-slim` avec les seuls extras nécessaires (formats PDF, DOCX,
   Markdown) ; version épinglée dans `uv.lock` ; torch depuis l'index CPU de
   PyTorch configuré dans `uv` ; une seule image backend en V1 ; poids des
   modèles dans un volume de cache.
7. **Garde-fou d'architecture** : `docling`, `docling_core`,
   `sentence_transformers`, `transformers`, `torch` et `numpy` sont ajoutés aux
   bibliothèques interdites dans `domain/` et `use_cases/`.

## Alternatives envisagées

- **Unstructured** : large couverture, mais dépendances très lourdes en mode
  haute résolution, sans avantage structurel sur Docling.
- **PyMuPDF / pymupdf4llm** : très rapide, mais licence AGPL (délicate pour un
  dépôt public) et structure sémantique limitée (code, hiérarchie). Reste un
  plan B possible pour un adapter PDF.
- **MarkItDown** : léger, mais pauvre sur PDF.
- **LlamaIndex IngestionPipeline** : docstore et cache propres, donc un second
  état concurrent de PostgreSQL ; le chunking pédagogique resterait à écrire.
- **LangChain** : splitters orientés caractères, pas de modèle d'ingestion
  durable.

## Conséquences

- Une seule représentation structurée pour tous les formats, donc un seul
  segmenteur.
- Les adapters sont remplaçables (PyMuPDF, autre modèle d'embeddings) sans
  toucher au domaine.
- L'équipe maintient elle-même l'orchestration ; elle existe de toute façon
  pour les besoins de fiabilité (ADR 0004).

## Risques et compromis

- Poids de Docling et de torch (taille d'image, RAM, temps de démarrage) :
  mesuré en S4 et S6.
- Rythme de publication élevé de Docling : version épinglée, montée de
  version testée par les golden tests.
- Licences des poids de modèles de layout : à revérifier au lock.
- Pas de bénéfice des intégrations prêtes à l'emploi des frameworks RAG.

## Conditions de réévaluation

- Besoin d'OCR (documents scannés) ou de HTML.
- Mesures de S6 inacceptables (temps par page, RAM).
- Besoin concret qu'un framework RAG couvrirait mieux, documenté avec la
  raison.

## Liens

- ADR [0006](0006-technical-bootstrap.md) (Qdrant, Celery/RabbitMQ — inchangés),
  [0007](0007-knowledge-base-bounded-context.md),
  [0009](0009-pedagogical-chunking-knowledge-units.md),
  [0011](0011-embedding-profile-search-index-blue-green.md).
- Conception : [knowledge-base.md §15](../conception/bounded-contexts/knowledge-base.md#15-pipeline-structure-du-code-et-feuille-de-route).

## Suivi (2026-11-18)

Faits d'installation mesurés en S3 (point 6, Proposed). Le corps de l'ADR est
inchangé.

- **Paquet** : `docling-slim[format-markdown]==2.150.1`, épinglé dans
  `backend/pyproject.toml` et `uv.lock` (ajouté par `uv add`). L'extra
  `format-markdown` ne tire que `marko` en plus du socle de `docling-slim` ;
  `docling-core` 2.107.1 apporte `pandas`, `pillow`, `lxml`, `jsonschema`,
  `typer`, `rich`. **Aucun `torch`** (ni `transformers`, ni modèle de layout)
  dans le lock : l'index CPU de PyTorch n'est pas configuré en S3, il le sera
  quand S4 (SentenceTransformers) ou S6 (PDF) le requerront.
- **Poids** : 26 paquets ajoutés (`numpy` était déjà présent) ; venv de
  développement 239,6 Mo → 333,0 Mo (+93,4 Mo, `du -sk`) ; image backend : voir
  le rapport de S3 (mesure `docker image inspect`, cible `development`).
- **Temps d'import** de `docling.document_converter` : 0,40 s à chaud
  (`python -X importtime`, macOS arm64, trois mesures 0,40 à 0,41 s), 17 s au
  premier import (compilation du bytecode). Conversion de la fixture
  `qa_python_fastapi_fr.md` (1,8 Ko) : 0,017 s. Docling n'est importé qu'à la
  première conversion d'un Markdown, dans le worker d'ingestion : l'API ne le
  charge pas.
- **TXT** : adapter natif (O7 tranché, [knowledge-base.md §16](../conception/bounded-contexts/knowledge-base.md#16-points-ouverts)).
- Reste à confirmer au lock de S6 : extras `format-pdf` / `format-docx`, poids
  de torch et licences des modèles de layout.


### S4 — torch et SentenceTransformers (2026-11-18)

Faits d'installation de S4 (point 6), le corps de l'ADR est inchangé.

- **Index CPU de PyTorch** déclaré dans `backend/pyproject.toml` :
  `[[tool.uv.index]]` `pytorch-cpu` (`https://download.pytorch.org/whl/cpu`,
  `explicit = true`) et `[tool.uv.sources] torch = { index = "pytorch-cpu" }`.
  `torch` est une dépendance directe (`>=2.15,<3`), pour que la source
  s'applique. Le lock résout `torch 2.15.0` (macOS) et `2.15.0+cpu` (Linux),
  tous deux depuis cet index ; **aucun paquet `nvidia-*`** dans `uv.lock`, ni
  dans l'image (vérifié dans `site-packages`).
- **Paquets** : `sentence-transformers 6.3.0` (`>=6.3,<7`), `transformers
  5.23.0`, `tokenizers 0.23.3`, `huggingface-hub 2.4.0`, `safetensors 0.8.0`,
  `scipy 1.18.1`, `scikit-learn 1.9.1` ; 24 paquets ajoutés (`uv add`).
- **Poids** (`du -sk`) : venv de développement macOS arm64 357 768 Ko →
  1 205 216 Ko ; dans l'image Linux arm64, `/app/.venv` 485 436 Ko →
  1 693 940 Ko (cible `development`) et 374 564 Ko → 1 584 184 Ko (cible
  `production`), dont `torch` 703 700 Ko. Image (`docker image inspect`,
  taille du contenu) : `development` 341 Mo → 995 Mo, `production` 152 Mo →
  479 Mo ; espace disque décompressé (`docker images`) : 1,54 Go → 4,67 Go et
  699 Mo → 2,27 Go.
- **Modèle** : `intfloat/multilingual-e5-base` à la révision épinglée
  `d128750597153bb5987e10b1c3493a34e5a4502a` (profil `MULTILINGUAL_E5_BASE`) ;
  `model.safetensors` 1 112 201 288 octets, cache complet 1 109 328 Ko.
  Téléchargé au premier chargement dans `KB_MODELS_CACHE_DIR` (volume
  `models_cache`), puis relu depuis le cache.
- **Téléchargement** : le protocole *xet* du Hub (`hf-xet`) est resté bloqué à
  0 octet, sur l'hôte comme dans le conteneur, alors que le téléchargement
  HTTPS aboutit : Compose pose `HF_HUB_DISABLE_XET=1` par défaut.
- **Coût mesuré** : voir le suivi de l'[ADR 0011](0011-embedding-profile-search-index-blue-green.md#suivi-2026-11-18)
  (chargement, RAM, temps par chunk) et le point O5 de
  [knowledge-base.md §16](../conception/bounded-contexts/knowledge-base.md#16-points-ouverts).


### S6 — Docling PDF/DOCX (2026-11-18)

Faits d'installation de S6 (point 6, confirmé au lock), le corps de l'ADR est
inchangé.

- **Extras** : `docling-slim[convert-core,format-docx,format-markdown,format-pdf,models-local]==2.150.1`
  (version gardée). `opencv-python-headless` (`>=5.1,<6`, requis par
  TableFormer, absent des extras ; variante sans X11) et `torchvision`
  (`>=0.29,<1`) sont des dépendances directes ; `torchvision` vient de l'index
  CPU de PyTorch (`[tool.uv.sources]`, comme `torch`). Lock : `torch`
  2.15.0 / 2.15.0+cpu et `torchvision` 0.29.2 / 0.29.2+cpu, tous depuis
  `download.pytorch.org/whl/cpu` ; **aucun paquet `nvidia-*`** dans `uv.lock`
  ni dans l'image (vérifié dans `site-packages`).
- **Paquets ajoutés** (`uv add`) : `docling-ibm-models` 4.0.4,
  `docling-parse` 7.26.0, `pypdfium2` 5.15.2, `python-docx` 1.2.0, `rtree`
  1.4.1, `accelerate` 1.16.0, `psutil` 7.2.2, `opencv-python-headless`
  5.1.0.95, `torchvision` 0.29.2, `pywin32` (Windows seulement). **Rétrogradé**
  : `huggingface-hub` 2.4.0 → 1.33.0, imposé par `docling-ibm-models`
  (`huggingface_hub<2`) ; `transformers` 5.23 et `sentence-transformers` 6.3
  l'acceptent (`>=1.31` et `>=1.3`), la révision épinglée d'E5 se charge
  toujours depuis le cache (tests `model` verts). Retirés avec lui :
  `httpx2`, `httpcore2`, `httpx2-jsfetch`, `truststore`.
- **Poids** (`du -sk`) : venv de développement macOS arm64 1 279 808 →
  1 398 588 Ko ; dans l'image Linux arm64, `/app/.venv` 1 694 252 →
  1 846 044 Ko (`development`) et 1 584 496 → 1 736 288 Ko (`production`).
  Image (`docker image inspect`, taille du contenu, polices comprises) :
  `production` 479 954 001 → 557 530 313 octets (480 → 558 Mo, +16 %),
  `development` 996 139 769 → 1 135 380 615 octets (996 Mo → 1,14 Go) ;
  espace disque décompressé (`docker images`) : 2,27 → 2,53 Go et
  4,67 → 5,15 Go.
- **Polices** : `fonts-urw-base35` et `fonts-dejavu-core` dans l'image
  (étapes `base` et `production`, 19 980 Ko sous `/usr/share/fonts`) :
  docling-parse rend lui-même les pages que lit le modèle de layout, et sans
  police système les polices standard non embarquées sortaient en rectangles
  (titres et code non reconnus dans le conteneur, mesuré). Détail :
  [README du contexte](../../backend/src/knowledge_base/README.md#s6--extraction-pdfdocx-avec-docling).
- **Modèles et licences** (revérifiés au lock, cartes de modèles du Hub) :
  `docling-project/docling-layout-heron` (RT-DETR, **Apache-2.0**), révision
  épinglée par le parseur (`8f39ad3c0b4c58e9c2d2c84a38465abf757272d8`,
  Docling suit `main`), 167 776 Ko ; `docling-project/docling-models` tag
  `v2.3.0` (TableFormer, **CDLA-Permissive-2.0**), épinglé par Docling,
  349 924 Ko (variantes `fast` et `accurate`). Les deux licences sont
  permissives et compatibles avec un dépôt public. Pas d'OCR (EasyOCR,
  RapidOCR) installé.
- **Cache** : cache du Hub (`HF_HOME`) dans le volume `models_cache`
  (`/var/cache/interview-rag-trainer/models/huggingface`), monté sur
  `worker_ingestion` (et sur `api`, qui ne convertit jamais de PDF) ;
  téléchargement au premier PDF : 176,7 s dans le conteneur, 112,6 s sur
  l'hôte (réseau du jour).
- **Confinement** : `docling`, `docling_core` et `pypdfium2` ne sont importés
  que par `adapters/gateway/parsing/docling_*.py` (test d'architecture) ;
  `pypdfium2` rejoint les bibliothèques interdites dans `domain/` et
  `use_cases/`.
- **Coût mesuré** : [ADR 0012, suivi S6](0012-original-document-storage-and-purge.md#s6--limites-mesurées-2026-11-18).
