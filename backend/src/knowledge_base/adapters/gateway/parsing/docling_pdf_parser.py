"""PDF natif -> ``StructuredDocument`` par le pipeline PDF de Docling (layout,
structure des tableaux), OCR désactivé (ADR 0008, point 5 ; roadmap S6).

Avant le pipeline de layout, qui charge des modèles et coûte de l'ordre d'une
seconde par page, le PDF est ouvert par pypdfium2 (le moteur PDF de Docling)
pour les échecs déterministes, tous terminaux :

- illisible, chiffré ou corrompu : ``INVALID_DOCUMENT`` ;
- plus de ``max_pages`` pages (300 par défaut, ADR 0012, point 4) :
  ``TOO_MANY_PAGES`` ;
- aucune page avec une couche texte (scan) : ``OCR_REQUIRED``. Un PDF
  partiellement scanné est extrait : ses pages textuelles seulement, le nombre
  de pages sans texte est journalisé.

La traduction des blocs est celle de ``docling_mapping`` : un PDF n'a aucune
hiérarchie de titres (tous de niveau 2), chaque bloc porte sa première page.

Docling, pypdfium2 et les modèles sont chargés à la première conversion, pas à
l'import du module ni à la construction : l'API compose le handler
d'extraction sans eux, et chaque processus enfant du worker charge les siens.
Les conversions d'un parseur sont sérialisées (``docling_conversion``).
Les poids sont téléchargés au premier usage dans le cache du Hub
(``HF_HOME``, volume ``models_cache`` sous Compose).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Final

from knowledge_base.adapters.gateway.parsing.docling_conversion import (
    DoclingConversion,
    document_with_blocks,
)
from knowledge_base.domain.exceptions.parsing_errors import (
    INVALID_DOCUMENT,
    OCR_REQUIRED,
    TOO_MANY_PAGES,
    DocumentParseError,
)
from knowledge_base.domain.value_objects.structured_document import (
    StructuredDocument,
)

if TYPE_CHECKING:
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter
    from pypdfium2 import PdfPage

logger = logging.getLogger(__name__)

MAX_PDF_PAGES: Final = 300
# Révision épinglée du modèle de layout (Docling suit « main ») : les sorties
# enregistrées des tests ne doivent changer qu'avec un changement relu. Licence
# Apache-2.0. TableFormer est épinglé par Docling (tag v2.3.0, CDLA-Permissive-2.0).
LAYOUT_MODEL_REVISION: Final = "8f39ad3c0b4c58e9c2d2c84a38465abf757272d8"


class DoclingPdfParser:
    def __init__(self, *, max_pages: int = MAX_PDF_PAGES) -> None:
        self._max_pages = max_pages
        self._conversion = DoclingConversion(".pdf", _pdf_converter)

    def parse(
        self, content: bytes, media_type: str, filename: str
    ) -> StructuredDocument:
        pages, pages_without_text = self._check_before_layout(content, filename)
        if pages_without_text:
            logger.warning(
                "%s : %d page(s) sans couche texte sur %d, ignorées (OCR désactivé)",
                filename,
                pages_without_text,
                pages,
            )
        started = time.perf_counter()
        converted = self._conversion.convert(content, filename)
        logger.info(
            "%s : %d page(s) converties par Docling en %.1f s",
            filename,
            pages,
            time.perf_counter() - started,
        )
        return document_with_blocks(converted, filename)

    def _check_before_layout(self, content: bytes, filename: str) -> tuple[int, int]:
        """Nombre de pages et de pages sans couche texte ; lève les échecs
        déterministes (illisible, trop de pages, aucun texte). Quelques
        millisecondes pour 300 pages, sans modèle."""
        import pypdfium2
        from docling.utils.locks import pypdfium2_lock

        # PDFium n'est pas réentrant : Docling sérialise ses appels par ce verrou.
        with pypdfium2_lock:
            try:
                pdf = pypdfium2.PdfDocument(content)
            except pypdfium2.PdfiumError as error:
                raise DocumentParseError(
                    INVALID_DOCUMENT,
                    f"{filename} : PDF illisible, chiffré ou corrompu ({error})",
                ) from error
            try:
                pages = len(pdf)
                if pages > self._max_pages:
                    raise DocumentParseError(
                        TOO_MANY_PAGES,
                        f"{filename} : {pages} pages, au-delà de la limite de "
                        f"{self._max_pages} pages",
                    )
                without_text = sum(
                    1 for index in range(pages) if not _has_text(pdf[index])
                )
            finally:
                pdf.close()
        if pages and without_text == pages:
            raise DocumentParseError(
                OCR_REQUIRED,
                f"{filename} : aucune couche texte sur ses {pages} page(s) "
                "(document scanné ?), l'OCR est désactivé",
            )
        return pages, without_text


def _has_text(page: PdfPage) -> bool:
    """Vrai si la page porte au moins un caractère visible dans sa couche
    texte ; la page est fermée ici."""
    try:
        text_page = page.get_textpage()
        try:
            return bool(text_page.get_text_range().strip())
        finally:
            text_page.close()
    finally:
        page.close()


def pdf_pipeline_options() -> PdfPipelineOptions:
    """Options du pipeline PDF de production (vérifiées par les tests : OCR
    désactivé, CPU, révision de layout épinglée)."""
    from docling.datamodel.accelerator_options import (
        AcceleratorDevice,
        AcceleratorOptions,
    )
    from docling.datamodel.pipeline_options import (
        LayoutObjectDetectionOptions,
        PdfPipelineOptions,
    )

    layout = LayoutObjectDetectionOptions.from_preset("layout_heron_default")
    layout.model_spec = layout.model_spec.model_copy(
        update={"revision": LAYOUT_MODEL_REVISION}
    )
    return PdfPipelineOptions(
        do_ocr=False,
        do_table_structure=True,
        layout_options=layout,
        # CPU explicite : même résultat sur l'hôte (qui offrirait MPS) que dans
        # le conteneur, et aucune dépendance à un GPU (ADR 0008).
        accelerator_options=AcceleratorOptions(device=AcceleratorDevice.CPU),
        # Une page à la fois par modèle, quatre pages au plus en attente entre
        # deux étapes (défauts Docling : lots de 4, file de 100). Mesuré sur un
        # PDF textuel de 300 pages (hôte, 2026-11-18) : pic de RSS 5 815 ->
        # 2 346 Mio, CPU 699 -> 343 s, durée 168 -> 207 s (ADR 0012, suivi).
        layout_batch_size=1,
        table_batch_size=1,
        queue_max_size=4,
    )


def _pdf_converter() -> DocumentConverter:
    from docling.datamodel.base_models import InputFormat
    from docling.document_converter import DocumentConverter, PdfFormatOption

    return DocumentConverter(
        allowed_formats=[InputFormat.PDF],
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_pipeline_options())
        },
    )
