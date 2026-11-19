"""Constantes partagées par les tests rouges de S8 (réconciliation, erreurs).

Le code d'échec de la borne de volume de texte (O16) est UNE décision ouverte :
``DOCUMENT_TOO_COMPLEX`` (existant, imbrication et XML décompressé) ou un code
dédié. Les tests retiennent un code dédié, ``DOCUMENT_TOO_LARGE`` ; changer
d'avis revient à modifier cette seule constante (et le catalogue).
"""

from __future__ import annotations

from typing import Final

TEXT_VOLUME_CODE: Final = "DOCUMENT_TOO_LARGE"

# Items de la work queue de S8.
RECONCILE_KIND: Final = "reconcile_knowledge_base"
INGESTION_QUEUE: Final = "knowledge_base.ingestion"
RECONCILE_TASK: Final = "knowledge_base.work.reconcile"
