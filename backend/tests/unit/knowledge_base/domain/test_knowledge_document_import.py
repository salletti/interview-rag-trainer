"""Création, import de versions : I1, I2, I3, I6 (knowledge-base.md §4.1).

API figée (agrégat ``KnowledgeDocument``, module
``knowledge_base.domain.entities.knowledge_document``) :

- ``KnowledgeDocument.import_new(*, document_id, version_id, title, kind,
  topics, language, source)`` -> ``KnowledgeDocument`` ACTIVE avec la version 1
  ``RECEIVED`` ; ``created_at`` de la version = ``source.imported_at`` (horloge
  injectée par l'appelant) ; ids fournis par l'appelant (UUID).
- ``document.import_version(*, version_id, source)`` -> ``DocumentVersion``.
- ``document.versions`` (tuple, ordre croissant de numéro),
  ``document.get_version(version_id)``, ``document.pull_events()`` (rend la
  liste puis la vide). ``document.version`` : compteur de verrou optimiste, 0 à
  la création, incrémenté par la persistance (S2), jamais par le domaine.
- ``KnowledgeDocument.restore(...)`` : réhydratation par la persistance, sans
  événement ni validation de transition.
- ``DocumentVersion`` : dataclass figée (``version_id``, ``number``, ``source``,
  ``status``, ``failure``, ``created_at``) ; les transitions remplacent l'objet
  dans l'agrégat (``dataclasses.replace``), aucun setter.
- Erreurs (module ``domain.exceptions.document_errors``, base
  ``KnowledgeDocumentError``) : ``DuplicateContentError`` (I3, choix :
  exception, pas de résultat « déjà importé »), ``DocumentWithdrawnError``,
  ``VersionNotFoundError``, ``InvalidVersionTransitionError``,
  ``StalePublicationError``.
- Événements (``domain.events.document_events``, dataclasses figées) :
  ``DocumentImported(document_id, version_id, number, content_hash)``,
  ``DocumentVersionDiscarded(document_id, version_id, number,
  replaced_by_version_id)``. Ordre à l'import de N+1 : ``DocumentImported`` puis
  un ``DocumentVersionDiscarded`` par version écartée, par numéro croissant.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from knowledge_base.domain.entities.knowledge_document import (
    DocumentStatus,
    KnowledgeDocument,
    VersionStatus,
)
from knowledge_base.domain.events.document_events import (
    DocumentImported,
    DocumentVersionDiscarded,
)
from knowledge_base.domain.exceptions.document_errors import (
    DuplicateContentError,
    KnowledgeDocumentError,
)
from knowledge_base.domain.value_objects.document_kind import DocumentKind
from tests.unit.knowledge_base.domain.builders import (
    DOCUMENT_ID,
    content_hash,
    extract,
    import_next,
    new_document,
    publish_numbered,
    restored_document,
    source,
    version_id,
)


def statuses(document: KnowledgeDocument) -> dict[int, VersionStatus]:
    return {v.number: v.status for v in document.versions}


# --- création -----------------------------------------------------------------


def test_import_new_creates_an_active_document_with_a_received_version_1() -> None:
    document = KnowledgeDocument.import_new(
        document_id=DOCUMENT_ID,
        version_id=version_id(1),
        title="Python avancé",
        kind=DocumentKind.QA_SHEET,
        topics=("python", "asyncio"),
        language="fr",
        source=source(1),
    )

    assert document.document_id == DOCUMENT_ID
    assert document.title == "Python avancé"
    assert document.kind is DocumentKind.QA_SHEET
    assert document.topics == ("python", "asyncio")
    assert document.language == "fr"
    assert document.status is DocumentStatus.ACTIVE
    assert document.published_version_id is None
    assert document.visibility_revision == 0
    assert document.version == 0
    (version,) = document.versions
    assert version.version_id == version_id(1)
    assert version.number == 1
    assert version.status is VersionStatus.RECEIVED
    assert version.failure is None
    assert version.source == source(1)
    assert version.created_at == source(1).imported_at


def test_import_new_rejects_a_blank_title() -> None:
    with pytest.raises(ValueError, match="title"):
        KnowledgeDocument.import_new(
            document_id=DOCUMENT_ID,
            version_id=version_id(1),
            title="  ",
            kind=DocumentKind.COURSE,
            topics=(),
            language="fr",
            source=source(1),
        )


def test_import_new_records_document_imported() -> None:
    document = KnowledgeDocument.import_new(
        document_id=DOCUMENT_ID,
        version_id=version_id(1),
        title="Python avancé",
        kind=DocumentKind.COURSE,
        topics=(),
        language="fr",
        source=source(1),
    )

    assert document.pull_events() == [
        DocumentImported(
            document_id=DOCUMENT_ID,
            version_id=version_id(1),
            number=1,
            content_hash=content_hash(1),
        )
    ]


# --- I2 : numéros strictement croissants ----------------------------------------


def test_versions_are_numbered_1_2_3_in_import_order() -> None:
    document = new_document()

    second = import_next(document, 2)
    third = import_next(document, 3)

    assert [v.number for v in document.versions] == [1, 2, 3]
    assert (second.number, third.number) == (2, 3)
    assert second.status is VersionStatus.RECEIVED


def test_numbers_are_never_reused_after_versions_were_discarded() -> None:
    document = new_document()
    import_next(document, 2)  # v1 DISCARDED
    import_next(document, 3)  # v2 DISCARDED

    fourth = import_next(document, 4)

    assert fourth.number == 4


def test_number_continues_after_the_highest_even_when_restored() -> None:
    document = restored_document(
        [(1, VersionStatus.DISCARDED), (5, VersionStatus.PUBLISHED)], published=5
    )

    assert import_next(document, 9).number == 6


# --- I3 : une empreinte par document ---------------------------------------------


def test_importing_the_same_content_hash_twice_is_refused() -> None:
    document = new_document(seed=1)

    with pytest.raises(DuplicateContentError):
        document.import_version(version_id=version_id(2), source=source(1))

    assert [v.number for v in document.versions] == [1]
    assert document.pull_events() == []


def test_a_duplicate_is_refused_even_if_the_original_was_discarded() -> None:
    document = new_document(seed=1)
    import_next(document, 2)  # v1 (hash 1) DISCARDED
    document.pull_events()

    with pytest.raises(DuplicateContentError):
        document.import_version(version_id=version_id(3), source=source(1))

    assert [v.number for v in document.versions] == [1, 2]
    assert statuses(document)[2] is VersionStatus.RECEIVED  # rien n'est écarté
    assert document.pull_events() == []


def test_duplicate_error_is_a_domain_error() -> None:
    assert issubclass(DuplicateContentError, KnowledgeDocumentError)


# --- I6 : N+1 écarte les versions antérieures non publiées ------------------------


@pytest.mark.parametrize(
    "previous",
    [VersionStatus.RECEIVED, VersionStatus.EXTRACTED, VersionStatus.FAILED],
)
def test_importing_n_plus_1_discards_the_previous_unpublished_version(
    previous: VersionStatus,
) -> None:
    document = restored_document([(1, previous)])

    document.import_version(version_id=version_id(2), source=source(2))

    assert statuses(document) == {1: VersionStatus.DISCARDED, 2: VersionStatus.RECEIVED}


def test_importing_n_plus_1_discards_every_older_unpublished_version() -> None:
    document = restored_document(
        [
            (1, VersionStatus.RECEIVED),
            (2, VersionStatus.EXTRACTED),
            (3, VersionStatus.FAILED),
        ]
    )

    document.import_version(version_id=version_id(4), source=source(4))

    assert statuses(document) == {
        1: VersionStatus.DISCARDED,
        2: VersionStatus.DISCARDED,
        3: VersionStatus.DISCARDED,
        4: VersionStatus.RECEIVED,
    }


def test_each_discarded_version_is_signalled_by_one_event() -> None:
    document = restored_document(
        [(1, VersionStatus.RECEIVED), (2, VersionStatus.FAILED)]
    )

    document.import_version(version_id=version_id(3), source=source(3))

    assert document.pull_events() == [
        DocumentImported(
            document_id=DOCUMENT_ID,
            version_id=version_id(3),
            number=3,
            content_hash=content_hash(3),
        ),
        DocumentVersionDiscarded(
            document_id=DOCUMENT_ID,
            version_id=version_id(1),
            number=1,
            replaced_by_version_id=version_id(3),
        ),
        DocumentVersionDiscarded(
            document_id=DOCUMENT_ID,
            version_id=version_id(2),
            number=2,
            replaced_by_version_id=version_id(3),
        ),
    ]


def test_the_published_version_survives_the_import_of_a_newer_one() -> None:
    document = new_document()
    publish_numbered(document, 1)
    document.pull_events()

    import_next(document, 2)

    assert statuses(document) == {1: VersionStatus.PUBLISHED, 2: VersionStatus.RECEIVED}
    assert document.published_version_id == version_id(1)
    assert [type(e) for e in document.pull_events()] == [DocumentImported]


def test_a_published_version_stays_while_older_unpublished_ones_are_discarded() -> None:
    document = restored_document(
        [(1, VersionStatus.PUBLISHED), (2, VersionStatus.EXTRACTED)], published=1
    )

    document.import_version(version_id=version_id(3), source=source(3))

    assert statuses(document) == {
        1: VersionStatus.PUBLISHED,
        2: VersionStatus.DISCARDED,
        3: VersionStatus.RECEIVED,
    }


def test_terminal_versions_are_left_untouched_and_not_signalled() -> None:
    document = new_document()
    publish_numbered(document, 1)
    import_next(document, 2)
    publish_numbered(document, 2)  # v1 SUPERSEDED
    import_next(document, 3)  # v1 SUPERSEDED, v2 PUBLISHED : rien à écarter
    document.pull_events()
    import_next(document, 4)  # v3 écartée

    assert statuses(document) == {
        1: VersionStatus.SUPERSEDED,
        2: VersionStatus.PUBLISHED,
        3: VersionStatus.DISCARDED,
        4: VersionStatus.RECEIVED,
    }
    discarded = [
        e for e in document.pull_events() if isinstance(e, DocumentVersionDiscarded)
    ]
    assert [e.number for e in discarded] == [3]


def test_no_discard_is_signalled_when_no_older_version_is_open() -> None:
    document = restored_document([(1, VersionStatus.DISCARDED)])

    import_next(document, 2)

    assert [type(e) for e in document.pull_events()] == [DocumentImported]


# --- I1 : immutabilité --------------------------------------------------------------


@pytest.mark.parametrize(
    "field", ["version_id", "number", "source", "status", "failure"]
)
def test_a_document_version_has_no_setter(field: str) -> None:
    version = new_document().versions[0]

    with pytest.raises(FrozenInstanceError):
        setattr(version, field, None)


def test_source_number_and_hash_survive_every_transition_unchanged() -> None:
    document = new_document()
    original = document.versions[0]
    extract(document, 1)
    extracted = document.get_version(version_id(1))
    import_next(document, 2)  # v1 EXTRACTED -> DISCARDED
    discarded = document.get_version(version_id(1))

    for later in (extracted, discarded):
        assert later.version_id == original.version_id
        assert later.number == original.number
        assert later.source == original.source
        assert later.created_at == original.created_at


def test_versions_is_a_read_only_tuple() -> None:
    document = new_document()

    assert isinstance(document.versions, tuple)
