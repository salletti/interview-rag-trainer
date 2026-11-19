"""Traduction ``DoclingDocument`` -> ``StructuredDocument`` des formats de S6
(PDF et DOCX), sur des sorties de Docling ENREGISTRÉES.

Les JSON de ``fixtures/knowledge_base/office/recorded/`` sont les sorties
RÉELLES de Docling 2.150.1 (extras ``format-pdf`` / ``format-docx``, OCR
désactivé, structure des tableaux activée) pour les PDF et DOCX de
``office/`` ; ``record_docling_documents.py`` les régénère. Seul le test
``test_hand_built_*`` construit son document à la main (API
``DoclingDocument``), pour des étiquettes que les fixtures n'exercent pas
(en-têtes de page, listes) : il est marqué comme tel.

API supposée (nouveau module, seul avec les parseurs à importer Docling) :
``knowledge_base.adapters.gateway.parsing.docling_mapping.
structured_document_from_docling(document: DoclingDocument) ->
StructuredDocument``. ``metadata`` est vide (ni front-matter, ni propriétés).

Règles supposées, observées sur les sorties réelles :

- ``TitleItem`` -> ``Heading(level=1)`` ; ``SectionHeaderItem(level=n)`` ->
  ``Heading(level=min(n + 1, 6))`` (comme le Markdown : ``##`` = niveau 2).
  Un DOCX conserve ainsi Titre = 1 et « Titre 1 » = 2 ; un PDF n'a aucune
  hiérarchie (Docling donne ``level=1`` à tout, titre compris) : tous ses
  titres sont de niveau 2.
- ``CodeItem`` -> ``CodeBlock`` (texte tel quel ; langage en minuscules,
  ``None`` pour ``unknown``). Le texte de code d'un PDF a perdu ses retours à
  la ligne et son indentation : limite de Docling, non corrigée ici.
- ``TableItem`` -> ``Table``, une ligne ``| a | b |`` par rangée ;
  ``TextItem`` -> ``Paragraph`` ; liste -> ``ListBlock``.
- Le parcours suit l'ordre de lecture ET descend dans les enfants de tout
  nœud sauf un tableau : Docling imbrique le contenu d'un DOCX sous le titre
  qui le précède, le parcours des seuls groupes (celui du Markdown) n'en
  verrait que les titres. Les enfants d'un tableau (cellules mises en forme)
  sont déjà dans ses lignes : ``test_docling_table_mapping``.
- ``page`` = première page de provenance (``prov``) du bloc ; ``None`` sans
  provenance (DOCX : pas de pagination).
- Le contenu hors corps (en-têtes et pieds de page : couche ``furniture``) et
  les images sont ignorés.
"""

from __future__ import annotations

import re

from docling_core.types.doc import (
    BoundingBox,
    CodeLanguageLabel,
    ContentLayer,
    DocItemLabel,
    DoclingDocument,
    ProvenanceItem,
    Size,
)

from knowledge_base.adapters.gateway.parsing.docling_mapping import (
    structured_document_from_docling,
)
from knowledge_base.domain.services.knowledge_unit_segmenter import (
    KnowledgeUnitSegmenter,
)
from knowledge_base.domain.value_objects.chunking_policy import ChunkingPolicy
from knowledge_base.domain.value_objects.document_kind import DocumentKind
from knowledge_base.domain.value_objects.structured_document import (
    Block,
    CodeBlock,
    Heading,
    ListBlock,
    Paragraph,
    StructuredDocument,
    Table,
)
from tests.office_support import recorded_docling
from tests.unit.knowledge_base.domain.services.support import (
    COUNTER,
    TOKENIZER,
    VERSION_ID,
)

QUESTIONS = (
    "Question : À quoi servent les fixtures pytest ?",
    "Question : Comment exécuter le même test avec plusieurs jeux de données ?",
    "Question : Quand faut-il utiliser un mock et quand l'éviter ?",
    "Question : Comment vérifier qu'une fonction lève une exception ?",
    "Question : Comment séparer les tests lents des tests rapides ?",
    "Question : Qu'est-ce que la couverture de code ne garantit pas ?",
)


def mapped(name: str) -> tuple[Block, ...]:
    return structured_document_from_docling(recorded_docling(name)).blocks


def headings(blocks: tuple[Block, ...]) -> list[Heading]:
    return [b for b in blocks if isinstance(b, Heading)]


def squashed(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# --- DOCX ---------------------------------------------------------------------


def test_docx_headings_keep_the_markdown_levels_title_1_and_heading_1_as_2() -> None:
    found = headings(mapped("qa_pytest_fr.docx"))

    assert [(h.level, h.text) for h in found] == [
        (1, "Fiche de révision pytest"),
        *[(2, question) for question in QUESTIONS],
    ]


def test_docx_content_nested_under_headings_is_not_lost() -> None:
    blocks = mapped("cours_git_fr.docx")

    assert [type(b).__name__ for b in blocks] == [
        "Heading",  # titre
        "Heading", "Paragraph",  # Branches
        "Heading", "Paragraph", "Paragraph",  # Fusion et rebase (+ commandes)
        "Heading", "Paragraph",  # Annuler
        "Heading", "Paragraph",  # Mettre de côté
        "Heading", "Paragraph",  # Retrouver
        "Heading", "Paragraph",  # Récupérer
    ]  # fmt: skip


def test_a_docx_has_no_pages() -> None:
    blocks = mapped("qa_pytest_fr.docx")

    assert blocks
    assert all(block.page is None for block in blocks)


def test_docx_code_keeps_its_lines_and_a_recognised_language_in_lowercase() -> None:
    codes = [b for b in mapped("qa_pytest_fr.docx") if isinstance(b, CodeBlock)]

    assert [c.language for c in codes] == ["python", "python", None]
    assert codes[0].text == (
        "@pytest.fixture\ndef client():\n    with TestClient(app) as c:\n"
        "        yield c"
    )


def test_a_docx_table_gives_one_line_per_row() -> None:
    (table,) = [b for b in mapped("tableau.docx") if isinstance(b, Table)]

    assert table == Table(
        text=(
            "| Méthode | Idempotente | Sûre |\n"
            "| GET | oui | oui |\n"
            "| PUT | oui | non |\n"
            "| POST | non | non |"
        ),
        page=None,
    )


# --- PDF ----------------------------------------------------------------------


def test_every_pdf_heading_is_level_2_because_docling_infers_no_hierarchy() -> None:
    found = headings(mapped("qa_pytest_fr.pdf"))

    assert [h.text for h in found] == ["Fiche de révision pytest", *QUESTIONS]
    assert {h.level for h in found} == {2}


def test_pdf_blocks_carry_the_page_where_they_start() -> None:
    blocks = mapped("qa_pytest_fr.pdf")

    pages = [b.page for b in blocks]
    assert pages[:-2] == [1] * (len(blocks) - 2)
    assert pages[-2:] == [2, 2]  # dernière question et sa réponse, page 2
    assert isinstance(blocks[-2], Heading)
    assert blocks[-2].text == QUESTIONS[-1]


def test_pdf_code_is_a_code_block_without_language() -> None:
    codes = [b for b in mapped("qa_pytest_fr.pdf") if isinstance(b, CodeBlock)]

    assert [c.language for c in codes] == [None, None, None]
    assert squashed(codes[0].text) == (
        "@pytest.fixture def client(): with TestClient(app) as c: yield c"
    )
    assert squashed(codes[2].text) == (
        'with pytest.raises(ValueError, match="négatif"): racine_carree(-1)'
    )


def test_a_pdf_table_gives_one_line_per_row_on_its_page() -> None:
    blocks = mapped("tableau.pdf")

    assert blocks[0] == Heading(level=2, text="Méthodes HTTP", page=1)
    assert isinstance(blocks[1], Paragraph)
    assert blocks[2] == Table(
        text=(
            "| Méthode | Idempotente | Sûre |\n"
            "| GET | oui | oui |\n"
            "| PUT | oui | non |\n"
            "| POST | non | non |"
        ),
        page=1,
    )
    assert len(blocks) == 3


def test_a_multi_page_pdf_gives_each_block_its_page() -> None:
    blocks = mapped("pages.pdf")

    assert [(type(b).__name__, b.page) for b in blocks] == [
        ("Heading", 1),  # Cours paginé
        ("Heading", 1),  # Première section
        ("Paragraph", 1),
        ("Heading", 2),  # Deuxième section
        ("Paragraph", 2),
        ("Paragraph", 3),  # suite, sans titre
        ("Heading", 3),  # Troisième section
        ("Paragraph", 3),
    ]


def test_a_unit_spanning_two_pages_gets_page_start_and_page_end() -> None:
    """``page_start`` / ``page_end`` des unités (``knowledge_units``) viennent
    des pages des blocs : le mapping suffit à les rendre exacts."""
    document = structured_document_from_docling(recorded_docling("pages.pdf"))
    policy = ChunkingPolicy(version="s6-1", tokenizer=TOKENIZER, max_tokens=120)

    units = list(
        KnowledgeUnitSegmenter(COUNTER, policy).segment(
            document, kind=DocumentKind.COURSE, version_id=VERSION_ID
        )
    )

    assert [
        (u.section_path.parts[-1], u.page_span.start, u.page_span.end) for u in units
    ] == [
        ("Première section", 1, 1),
        ("Deuxième section", 2, 3),
        ("Troisième section", 3, 3),
    ]


def test_the_mapped_document_has_no_metadata() -> None:
    document = structured_document_from_docling(recorded_docling("qa_pytest_fr.pdf"))

    assert dict(document.metadata) == {}


# --- document construit à la main (API DoclingDocument) ---------------------------


def _prov(page: int) -> ProvenanceItem:
    return ProvenanceItem(
        page_no=page, bbox=BoundingBox(l=0, t=0, r=1, b=1), charspan=(0, 1)
    )


def hand_built_document() -> DoclingDocument:
    """CONSTRUIT À LA MAIN, pas enregistré : les fixtures n'ont ni en-tête de
    page, ni liste, ni image."""
    document = DoclingDocument(name="construit-a-la-main")
    for page in (1, 2):
        document.add_page(page_no=page, size=Size(width=595, height=842))
    document.add_text(
        label=DocItemLabel.PAGE_HEADER,
        text="EN-TETE REPETE",
        prov=_prov(1),
        content_layer=ContentLayer.FURNITURE,
    )
    document.add_heading("Listes", level=1, prov=_prov(1))
    group = document.add_list_group()
    document.add_list_item("premier", parent=group, prov=_prov(1))
    document.add_list_item("deuxième", parent=group, prov=_prov(2))
    document.add_code("print(1)", code_language=CodeLanguageLabel.PYTHON, prov=_prov(2))
    document.add_picture(prov=_prov(2))
    document.add_text(
        label=DocItemLabel.PAGE_FOOTER,
        text="PIED REPETE",
        prov=_prov(2),
        content_layer=ContentLayer.FURNITURE,
    )
    document.add_heading("Profond", level=6, prov=_prov(2))
    return document


def test_hand_built_page_headers_footers_and_pictures_are_dropped() -> None:
    blocks = structured_document_from_docling(hand_built_document()).blocks

    rendered = repr(blocks)
    assert "EN-TETE REPETE" not in rendered
    assert "PIED REPETE" not in rendered
    assert [type(b).__name__ for b in blocks] == [
        "Heading",
        "ListBlock",
        "CodeBlock",
        "Heading",
    ]


def test_hand_built_a_list_spanning_two_pages_starts_on_the_first() -> None:
    blocks = structured_document_from_docling(hand_built_document()).blocks

    assert blocks[1] == ListBlock(items=("premier", "deuxième"), page=1)


def test_hand_built_a_paragraph_split_across_pages_starts_on_its_first_page() -> None:
    """Docling rend UN paragraphe dont la provenance couvre plusieurs pages
    (observé sur un PDF dont un paragraphe passait de la page 2 à la page 5)."""
    document = DoclingDocument(name="paragraphe-coupe")
    for page in (2, 3):
        document.add_page(page_no=page, size=Size(width=595, height=842))
    paragraph = document.add_text(
        label=DocItemLabel.TEXT, text="Un paragraphe coupé.", prov=_prov(2)
    )
    paragraph.prov.append(_prov(3))

    blocks = structured_document_from_docling(document).blocks

    assert blocks == (Paragraph(text="Un paragraphe coupé.", page=2),)


def test_hand_built_code_language_is_lowercase_and_heading_level_is_capped() -> None:
    blocks = structured_document_from_docling(hand_built_document()).blocks

    assert blocks[2] == CodeBlock(text="print(1)", language="python", page=2)
    assert blocks[3] == Heading(level=6, text="Profond", page=2)  # min(6 + 1, 6)


def test_hand_built_a_document_without_body_content_has_no_blocks() -> None:
    """Le mapping ne décide pas du vide : c'est le parseur qui distingue
    ``OCR_REQUIRED`` de ``EMPTY_DOCUMENT``."""
    document = DoclingDocument(name="vide")

    assert structured_document_from_docling(document) == StructuredDocument(blocks=())
