"""Nom de fichier d'un import (revue S2 M-3) : réduit à son nom final (aucun
chemin, séparateurs ``/`` et ``\\``), refusé s'il est vide, s'il dépasse 255
caractères ou s'il contient un caractère de contrôle — avant toute écriture.
"""

from __future__ import annotations

import pytest

from knowledge_base.domain.value_objects.document_kind import DocumentKind
from knowledge_base.use_cases.errors import InvalidFilenameError
from knowledge_base.use_cases.import_document.import_document_command import (
    ImportDocument,
)
from knowledge_base.use_cases.import_document_version.import_document_version_command import (  # noqa: E501
    ImportDocumentVersion,
)
from knowledge_base.use_cases.import_steps import accepted_filename
from tests.unit.knowledge_base.use_cases.fakes import make_harness, stream

CONTENT = b"# Cours\n"


def command(filename: str) -> ImportDocument:
    return ImportDocument(
        title="Python avancé",
        kind=DocumentKind.COURSE,
        topics=(),
        language="fr",
        filename=filename,
        media_type="text/markdown",
        content=stream(CONTENT),
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("cours.md", "cours.md"),
        ("../../etc/cours.md", "cours.md"),
        ("/var/tmp/cours.md", "cours.md"),
        ("..\\..\\Windows\\cours.md", "cours.md"),
        ("C:\\Users\\moi\\cours.md", "cours.md"),
        ("dossier/sous\\cours.md", "cours.md"),
        ("é" * 255, "é" * 255),
    ],
)
def test_the_filename_is_reduced_to_its_final_name(raw: str, expected: str) -> None:
    assert accepted_filename(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "dossier/", "..", "a/..", ".", "C:\\", "x" * 256, "co\x00urs.md"],
)
def test_an_unusable_filename_is_refused(raw: str) -> None:
    with pytest.raises(InvalidFilenameError) as raised:
        accepted_filename(raw)

    assert raised.value.filename == raw


async def test_the_stored_source_keeps_only_the_final_name() -> None:
    harness = make_harness()

    result = await harness.import_document.handle(command("../../secret/cours.md"))

    document = harness.state.documents[result.document_id]
    assert document.get_version(result.version_id).source.filename == "cours.md"


async def test_the_same_file_under_another_path_replays_the_same_key() -> None:
    harness = make_harness()
    first = await harness.import_document.handle(
        ImportDocument(**{**vars(command("a/cours.md")), "idempotency_key": "k"})
    )

    again = await harness.import_document.handle(
        ImportDocument(**{**vars(command("b\\cours.md")), "idempotency_key": "k"})
    )

    assert again == first


async def test_an_invalid_filename_is_refused_before_any_write() -> None:
    harness = make_harness()

    with pytest.raises(InvalidFilenameError):
        await harness.import_document.handle(command("x" * 256 + ".md"))

    assert harness.blobs.put_calls == 0
    assert harness.state.documents == {}


async def test_a_version_with_an_invalid_filename_is_refused_before_any_write() -> None:
    harness = make_harness()
    first = await harness.import_document.handle(command("cours.md"))

    with pytest.raises(InvalidFilenameError):
        await harness.import_version.handle(
            ImportDocumentVersion(
                document_id=first.document_id,
                filename="dossier/",
                media_type="text/markdown",
                content=stream(b"# V2\n"),
            )
        )

    assert harness.blobs.put_calls == 1
    assert len(harness.state.documents[first.document_id].versions) == 1
