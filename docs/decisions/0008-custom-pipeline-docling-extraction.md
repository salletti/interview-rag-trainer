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
