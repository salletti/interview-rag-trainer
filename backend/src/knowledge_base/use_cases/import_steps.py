"""Étapes communes à ``ImportDocument`` et ``ImportDocumentVersion``
(knowledge-base.md §6, ligne « Import » ; 002, « Erreurs et idempotence »).

Ordre d'un import : contrôle du nom de fichier et du type (avant toute
écriture), dépôt du blob (hors transaction, ADR 0012), puis une transaction
unique : document et version, item ``extract_version``, annulation des items
des versions écartées (I6), clé d'idempotence.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import PurePath
from typing import Final
from uuid import UUID

from knowledge_base.domain.events.document_events import (
    DocumentEvent,
    DocumentVersionDiscarded,
)
from knowledge_base.domain.providers.idempotency_key_repository import (
    IdempotencyKeyTakenError,
    IdempotencyRecord,
)
from knowledge_base.domain.providers.knowledge_document_repository import (
    ConcurrentModificationError,
    ContentHashTakenError,
)
from knowledge_base.domain.providers.unit_of_work import UnitOfWork
from knowledge_base.use_cases.errors import (
    IdempotencyKeyConflictError,
    InvalidFilenameError,
    UnsupportedDocumentTypeError,
)
from knowledge_base.use_cases.import_result import (
    ImportDocumentResult,
)

logger = logging.getLogger(__name__)

EXTRACT_VERSION: Final = "extract_version"
MAX_FILENAME_LENGTH: Final = 255

# Liste blanche de S2 : le type de média ET l'extension doivent concorder.
_SUPPORTED_TYPES: Final[Mapping[str, frozenset[str]]] = {
    "text/markdown": frozenset({".md", ".markdown"}),
    "text/plain": frozenset({".txt"}),
}


def utc_now() -> datetime:
    return datetime.now(UTC)


def extract_version_key(version_id: UUID) -> str:
    return f"{EXTRACT_VERSION}:{version_id}"


def accepted_filename(filename: str) -> str:
    """Nom final du fichier, sans aucun chemin (séparateurs ``/`` et ``\\``) :
    le nom fourni par le client n'est qu'une métadonnée affichée, il ne doit
    jamais porter de chemin. ``InvalidFilenameError`` si rien d'utilisable ne
    reste (``dossier/``, ``..``), si le nom dépasse 255 caractères ou contient
    un caractère de contrôle."""
    # Découpage explicite plutôt que PureWindowsPath : celui-ci normalise
    # « dossier/ » en « dossier », qui n'est pas le nom d'un fichier.
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    if not name.strip() or name in {".", ".."}:
        raise InvalidFilenameError(filename, "nom de fichier vide")
    if len(name) > MAX_FILENAME_LENGTH:
        raise InvalidFilenameError(
            filename, f"nom de fichier de plus de {MAX_FILENAME_LENGTH} caractères"
        )
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in name):
        raise InvalidFilenameError(filename, "caractère de contrôle")
    return name


def accepted_media_type(filename: str, media_type: str) -> str:
    """Type de média sans ses paramètres (``; charset=…``), si le couple
    type / extension est autorisé ; ``UnsupportedDocumentTypeError`` sinon."""
    essence = media_type.split(";", 1)[0].strip().lower()
    extension = PurePath(filename).suffix.lower()
    if extension not in _SUPPORTED_TYPES.get(essence, frozenset()):
        raise UnsupportedDocumentTypeError(filename, media_type)
    return essence


def request_fingerprint(fields: Mapping[str, str | list[str] | None]) -> str:
    """Empreinte d'une requête d'import : SHA-256 d'une sérialisation JSON
    canonique, indépendante de l'ordre de déclaration des champs."""
    canonical = json.dumps(fields, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def replay(
    uow: UnitOfWork, key: str | None, fingerprint: str
) -> ImportDocumentResult | None:
    """Résultat enregistré pour cette clé, ou ``None`` si la clé est absente
    ou inconnue. Même clé, autre requête : ``IdempotencyKeyConflictError``."""
    if key is None:
        return None
    record = await uow.idempotency_keys.get(key)
    if record is None:
        return None
    if record.fingerprint != fingerprint:
        raise IdempotencyKeyConflictError(key)
    return ImportDocumentResult(
        document_id=record.document_id,
        version_id=record.version_id,
        number=record.number,
        created=record.created,
    )


async def remember(
    uow: UnitOfWork, key: str | None, fingerprint: str, result: ImportDocumentResult
) -> None:
    if key is None:
        return
    await uow.idempotency_keys.add(
        IdempotencyRecord(
            key=key,
            fingerprint=fingerprint,
            document_id=result.document_id,
            version_id=result.version_id,
            number=result.number,
            created=result.created,
        )
    )


async def schedule_extraction(
    uow: UnitOfWork,
    document_id: UUID,
    version_id: UUID,
    events: Iterable[DocumentEvent],
) -> None:
    """Planifie l'extraction de la nouvelle version et annule celle de chaque
    version écartée (I6), dans la transaction de l'import."""
    await uow.work_queue.enqueue(
        EXTRACT_VERSION,
        extract_version_key(version_id),
        {"document_id": str(document_id), "version_id": str(version_id)},
    )
    for event in events:
        if isinstance(event, DocumentVersionDiscarded):
            await uow.work_queue.cancel(extract_version_key(event.version_id))


async def retry_once_after_race(
    attempt: Callable[[], Awaitable[ImportDocumentResult]],
) -> ImportDocumentResult:
    """Rejoue une fois la transaction d'import quand un import concurrent a
    commité entre notre lecture et notre écriture : même empreinte, même clé,
    ou nouvelle version du même document (verrou optimiste perdu). La seconde
    tentative relit tout et prend le chemin « rejeu » (même clé), « contenu
    connu » (ou ``DUPLICATE_CONTENT``), ou importe sur l'état à jour. Une
    seconde course perdue remonte (409 ``CONCURRENT_MODIFICATION``)."""
    try:
        return await attempt()
    except (
        ConcurrentModificationError,
        ContentHashTakenError,
        IdempotencyKeyTakenError,
    ) as race:
        logger.info("import concurrent détecté, relecture : %s", race)
        return await attempt()
