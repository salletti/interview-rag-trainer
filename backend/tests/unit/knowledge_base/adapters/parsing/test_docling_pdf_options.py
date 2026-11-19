"""Options du pipeline PDF de production (ADR 0008, point 5 ; roadmap S6) :
construites sans charger aucun modèle."""

from __future__ import annotations

from docling.datamodel.accelerator_options import AcceleratorDevice
from docling.datamodel.pipeline_options import LayoutObjectDetectionOptions

from knowledge_base.adapters.gateway.parsing.docling_pdf_parser import (
    LAYOUT_MODEL_REVISION,
    pdf_pipeline_options,
)


def test_ocr_is_disabled_and_tables_are_structured() -> None:
    options = pdf_pipeline_options()

    assert options.do_ocr is False
    assert options.do_table_structure is True


def test_the_pipeline_runs_on_cpu_with_the_pinned_layout_revision() -> None:
    options = pdf_pipeline_options()

    assert options.accelerator_options.device == AcceleratorDevice.CPU
    assert isinstance(options.layout_options, LayoutObjectDetectionOptions)
    assert options.layout_options.model_spec.revision == LAYOUT_MODEL_REVISION
    assert len(LAYOUT_MODEL_REVISION) == 40  # un commit, pas une branche


def test_pages_go_one_at_a_time_through_each_model() -> None:
    """Mesuré en S6 : les lots de 4 pages doublent le CPU et le pic de RSS."""
    options = pdf_pipeline_options()

    assert (options.layout_batch_size, options.table_batch_size) == (1, 1)
    assert options.queue_max_size == 4
