"""``DISCARDED`` contre ``SUPERSEDED`` et document retiré (S7 ; knowledge-base.md
§5.1, §5.2, §7.3 condition 5, §8).

API figée (complète S1, ``test_knowledge_document_withdrawal``) :

- ``DISCARDED`` : version abandonnée AVANT d'avoir été publiée ; ``SUPERSEDED`` :
  version qui a été publiée puis remplacée. Une version jamais publiée n'est
  donc jamais ``SUPERSEDED``, même quand une version plus récente est publiée
  par-dessus.
- ``record_extraction`` sur un document ``WITHDRAWN`` lève
  ``DocumentWithdrawnError`` sans aucun effet (version toujours ``RECEIVED``,
  aucun événement) : défense en profondeur de la condition 5 de §7.3, au cas où
  l'annulation du work item (fencing) n'aurait pas eu lieu. Report de S1 dans
  knowledge-base-progress.md (« ``record_extraction`` n'interdit pas un document
  ``WITHDRAWN`` »).
"""

from __future__ import annotations

import pytest

from knowledge_base.domain.entities.knowledge_document import VersionStatus
from knowledge_base.domain.exceptions.document_errors import DocumentWithdrawnError
from tests.unit.knowledge_base.domain.builders import (
    extract,
    import_next,
    new_document,
    publish_numbered,
    version_id,
)


def test_a_withdrawn_document_refuses_an_extraction_and_changes_nothing() -> None:
    document = new_document()
    document.withdraw()
    document.pull_events()

    with pytest.raises(DocumentWithdrawnError):
        document.record_extraction(version_id(1))

    assert document.get_version(version_id(1)).status is VersionStatus.RECEIVED
    assert document.pull_events() == []


def test_a_version_never_published_is_discarded_never_superseded() -> None:
    document = new_document()
    publish_numbered(document, 1)
    import_next(document, 2)
    extract(document, 2)  # EXTRACTED, jamais publiée
    import_next(document, 3)  # écarte la 2, la 1 reste publiée

    extract(document, 3)
    document.publish(version_id(3))

    statuses = {v.number: v.status for v in document.versions}
    assert statuses == {
        1: VersionStatus.SUPERSEDED,
        2: VersionStatus.DISCARDED,
        3: VersionStatus.PUBLISHED,
    }


def test_the_published_version_is_not_discarded_by_a_newer_import() -> None:
    document = new_document()
    publish_numbered(document, 1)

    import_next(document, 2)

    assert document.get_version(version_id(1)).status is VersionStatus.PUBLISHED
    assert document.published_version_id == version_id(1)
