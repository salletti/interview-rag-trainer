"""Génère, de façon reproductible, les fixtures PDF et DOCX de la slice S6.

Ce script n'est PAS une dépendance du projet et n'est pas collecté par pytest.
Il s'exécute dans un environnement éphémère (rien n'est installé sur l'hôte ni
dans le projet) ; les versions sont épinglées ici :

    cd backend && uv run --no-project \\
        --with reportlab==5.1.0 --with python-docx==1.2.0 \\
        python tests/fixtures/knowledge_base/generate_office_fixtures.py

Les sorties (``tests/fixtures/knowledge_base/office/``) sont commitées ; les
relancer doit produire des fichiers identiques octet pour octet (reportlab en
mode ``invariant``, horodatages du ZIP DOCX et propriétés du document figés).

Fixtures produites :

- ``qa_pytest_fr.{pdf,docx}`` et ``cours_git_fr.{pdf,docx}`` : le MÊME contenu
  que ``retrieval/qa_pytest_fr.md`` et ``retrieval/cours_git_fr.md`` (le
  Markdown est lu ici, front-matter exclu : un PDF n'en a pas) ;
- ``tableau.{pdf,docx}`` : un titre, un paragraphe et un tableau 4 x 3 ;
- ``pages.pdf`` : trois pages ; la deuxième section s'étend sur les pages 2 et 3
  (``page_start`` != ``page_end``) ;
- ``scanned.pdf`` : une page qui ne contient QU'UNE image (texte rasterisé),
  donc aucune couche texte : il faudrait de l'OCR ;
- ``encrypted.pdf`` : le contenu de ``qa_pytest_fr.pdf`` chiffré par reportlab
  (RC4 128 bits), mot de passe utilisateur ``secret`` (pypdf, envisagé, n'est
  pas nécessaire : son chiffrement n'est pas reproductible) ;
- ``corrupted.pdf`` : ``qa_pytest_fr.pdf`` tronqué (ni ``xref`` ni ``%%EOF``) ;
- ``cours_concurrence_long_fr.pdf`` (ajout de l'implémentation de S6) : le
  cours ``office/cours_concurrence_long_fr.md``, propre à S6 (hors du jeu de
  S5), dont la section « Exécuter du code bloquant… » dépasse le budget d'un
  chunk et ``max_passage_tokens`` : unité multi-chunks, passage tronqué, sur
  plusieurs pages (évaluation des unités longues).
"""

from __future__ import annotations

import importlib
import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
OUT = HERE / "office"
SOURCES = HERE / "retrieval"
PASSWORD = "secret"
ZIP_DATE = (2026, 11, 18, 0, 0, 0)

# Mise en page : assez grande pour que le modèle de layout reconnaisse titres,
# paragraphes et code (corps de texte 11 pt, code Courier 9 pt).
TITLE_SIZE, H2_SIZE, BODY_SIZE, CODE_SIZE = 22, 16, 11, 9


@dataclass(frozen=True)
class Block:
    kind: str  # "h1" | "h2" | "p" | "code"
    text: str


# --- lecture du Markdown de référence -------------------------------------------


def read_markdown(path: Path) -> list[Block]:
    """Sous-ensemble de Markdown des fixtures : front-matter ignoré, ``# `` et
    ``## ``, blocs de code ``` et paragraphes (une ligne = un paragraphe)."""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.DOTALL)
    blocks: list[Block] = []
    code: list[str] | None = None
    for line in text.split("\n"):
        if line.startswith("```"):
            if code is None:
                code = []
            else:
                blocks.append(Block("code", "\n".join(code)))
                code = None
        elif code is not None:
            code.append(line)
        elif line.startswith("## "):
            blocks.append(Block("h2", line[3:].strip()))
        elif line.startswith("# "):
            blocks.append(Block("h1", line[2:].strip()))
        elif line.strip():
            blocks.append(Block("p", line.strip()))
    return blocks


# --- PDF (reportlab) -------------------------------------------------------------


def _reportlab() -> tuple[Any, Any, Any, Any]:
    config: Any = importlib.import_module("reportlab.rl_config")
    config.invariant = 1  # dates et identifiants figés : sortie reproductible
    platypus = importlib.import_module("reportlab.platypus")
    styles = importlib.import_module("reportlab.lib.styles")
    pagesizes = importlib.import_module("reportlab.lib.pagesizes")
    return config, platypus, styles, pagesizes


def _pdf_styles(styles_module: Any) -> dict[str, Any]:
    base = styles_module.getSampleStyleSheet()
    parent = base["Normal"]
    make = styles_module.ParagraphStyle
    return {
        "h1": make(
            "h1",
            parent=parent,
            fontName="Helvetica-Bold",
            fontSize=TITLE_SIZE,
            leading=TITLE_SIZE + 6,
            spaceAfter=14,
        ),
        "h2": make(
            "h2",
            parent=parent,
            fontName="Helvetica-Bold",
            fontSize=H2_SIZE,
            leading=H2_SIZE + 4,
            spaceBefore=14,
            spaceAfter=8,
        ),
        "p": make(
            "p",
            parent=parent,
            fontName="Helvetica",
            fontSize=BODY_SIZE,
            leading=BODY_SIZE + 4,
            spaceAfter=8,
        ),
        "code": make(
            "code",
            parent=parent,
            fontName="Courier",
            fontSize=CODE_SIZE,
            leading=CODE_SIZE + 3,
            spaceAfter=10,
            leftIndent=12,
        ),
    }


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _flowables(blocks: list[Block], platypus: Any, styles: dict[str, Any]) -> list[Any]:
    flowables: list[Any] = []
    for block in blocks:
        if block.kind == "code":
            flowables.append(platypus.Preformatted(block.text, styles["code"]))
        else:
            flowables.append(
                platypus.Paragraph(_escape(block.text), styles[block.kind])
            )
    return flowables


def build_pdf(
    flowables: list[Any], platypus: Any, pagesizes: Any, **options: Any
) -> bytes:
    buffer = io.BytesIO()
    document = platypus.SimpleDocTemplate(
        buffer, pagesize=pagesizes.A4, title="fixture", author="fixture", **options
    )
    document.build(flowables)
    return buffer.getvalue()


# --- DOCX (python-docx) ----------------------------------------------------------


def build_docx(blocks: list[Block], table: list[list[str]] | None = None) -> bytes:
    docx = importlib.import_module("docx")
    document = docx.Document()
    for block in blocks:
        if block.kind == "h1":
            document.add_heading(block.text, level=0)  # style « Title »
        elif block.kind == "h2":
            document.add_heading(block.text, level=1)  # style « Heading 1 »
        elif block.kind == "code":
            paragraph = document.add_paragraph()
            run = paragraph.add_run(block.text)
            run.font.name = "Courier New"
        elif block.kind == "table" and table is not None:
            grid = document.add_table(rows=len(table), cols=len(table[0]))
            grid.style = "Table Grid"
            for row, values in zip(grid.rows, table, strict=True):
                for cell, value in zip(row.cells, values, strict=True):
                    cell.text = value
        else:
            document.add_paragraph(block.text)
    properties = document.core_properties
    properties.author = "fixture"
    properties.title = "fixture"
    from datetime import datetime

    fixed = datetime(2026, 11, 18)
    properties.created = properties.modified = properties.last_printed = fixed
    buffer = io.BytesIO()
    document.save(buffer)
    return _with_fixed_zip_dates(buffer.getvalue())


def _with_fixed_zip_dates(content: bytes) -> bytes:
    """python-docx horodate les entrées du ZIP à l'heure courante : les figer
    rend la sortie reproductible."""
    out = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(content)) as source,
        zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for info in source.infolist():
            clone = zipfile.ZipInfo(info.filename, date_time=ZIP_DATE)
            clone.compress_type = zipfile.ZIP_DEFLATED
            clone.external_attr = info.external_attr
            target.writestr(clone, source.read(info.filename))
    return out.getvalue()


# --- fixtures --------------------------------------------------------------------

TABLE = [
    ["Méthode", "Idempotente", "Sûre"],
    ["GET", "oui", "oui"],
    ["PUT", "oui", "non"],
    ["POST", "non", "non"],
]


def table_flowable(platypus: Any, styles_module: Any) -> Any:
    colors = importlib.import_module("reportlab.lib.colors")
    table = platypus.Table(TABLE, colWidths=[110, 110, 110])
    table.setStyle(
        platypus.TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.8, colors.black),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), BODY_SIZE),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return table


def scanned_pdf() -> bytes:
    """Une page dont le seul contenu est une image : aucune couche texte."""
    image_module = importlib.import_module("PIL.Image")
    draw_module = importlib.import_module("PIL.ImageDraw")
    canvas_module = importlib.import_module("reportlab.pdfgen.canvas")
    utils = importlib.import_module("reportlab.lib.utils")
    _reportlab()  # mode invariant
    image = image_module.new("RGB", (1240, 1754), "white")
    draw = draw_module.Draw(image)
    for row in range(12):
        draw.rectangle([120, 160 + row * 90, 1120, 190 + row * 90], fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    out = io.BytesIO()
    page = canvas_module.Canvas(out, pagesize=(595.27, 841.89), invariant=1)
    page.drawImage(utils.ImageReader(buffer), 0, 0, width=595.27, height=841.89)
    page.showPage()
    page.save()
    return out.getvalue()


def corrupted_pdf(content: bytes) -> bytes:
    """Début valide (en-tête, objets), puis plus rien : ni ``xref`` ni
    ``%%EOF``."""
    cut = content.rfind(b"xref")
    return content[: cut // 2]


def main() -> None:
    OUT.mkdir(exist_ok=True)
    _, platypus, styles_module, pagesizes = _reportlab()
    styles = _pdf_styles(styles_module)
    written: dict[str, bytes] = {}

    for name in ("qa_pytest_fr", "cours_git_fr"):
        blocks = read_markdown(SOURCES / f"{name}.md")
        written[f"{name}.pdf"] = build_pdf(
            _flowables(blocks, platypus, styles), platypus, pagesizes
        )
        written[f"{name}.docx"] = build_docx(blocks)

    intro = [
        Block("h1", "Méthodes HTTP"),
        Block("p", "Le tableau ci-dessous résume les propriétés des méthodes HTTP."),
    ]
    written["tableau.pdf"] = build_pdf(
        [*_flowables(intro, platypus, styles), table_flowable(platypus, styles_module)],
        platypus,
        pagesizes,
    )
    written["tableau.docx"] = build_docx([*intro, Block("table", "")], TABLE)

    page_break = platypus.PageBreak()

    def page(*blocks: Block) -> list[Any]:
        return _flowables(list(blocks), platypus, styles)

    # Page 1 : titre et première section ; page 2 : deuxième section ; page 3 :
    # suite de la deuxième section (nouveau paragraphe, sans titre), puis
    # troisième section. La deuxième section s'étend donc sur les pages 2 à 3.
    paged = [
        *page(
            Block("h1", "Cours paginé"),
            Block("h2", "Première section"),
            Block("p", "Texte de la première page."),
        ),
        page_break,
        *page(
            Block("h2", "Deuxième section"),
            Block("p", "Début de la deuxième section, sur la deuxième page."),
        ),
        page_break,
        *page(
            Block("p", "Fin de la deuxième section, sur la troisième page."),
            Block("h2", "Troisième section"),
            Block("p", "Texte de la troisième section."),
        ),
    ]
    written["pages.pdf"] = build_pdf(paged, platypus, pagesizes)

    written["scanned.pdf"] = scanned_pdf()
    pdfencrypt = importlib.import_module("reportlab.lib.pdfencrypt")
    written["encrypted.pdf"] = build_pdf(
        _flowables(read_markdown(SOURCES / "qa_pytest_fr.md"), platypus, styles),
        platypus,
        pagesizes,
        encrypt=pdfencrypt.StandardEncryption(PASSWORD, canPrint=1),
    )
    written["corrupted.pdf"] = corrupted_pdf(written["qa_pytest_fr.pdf"])
    written["cours_concurrence_long_fr.pdf"] = build_pdf(
        _flowables(
            read_markdown(OUT / "cours_concurrence_long_fr.md"), platypus, styles
        ),
        platypus,
        pagesizes,
    )

    for filename, content in written.items():
        (OUT / filename).write_bytes(content)
        print(f"{filename}: {len(content)} octets")  # noqa: T201


if __name__ == "__main__":
    main()
