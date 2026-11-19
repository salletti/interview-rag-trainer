"""Lecture du nom de la contrainte violée dans une ``IntegrityError``, pour
traduire les seules violations attendues en exceptions des ports."""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError


def violated_constraint(error: IntegrityError) -> str | None:
    """Nom de la contrainte, porté par l'exception asyncpg d'origine
    (``constraint_name``), que l'adaptateur DBAPI de SQLAlchemy chaîne en
    ``__cause__``."""
    origin = getattr(error.orig, "__cause__", None)
    name = getattr(origin, "constraint_name", None)
    return name if isinstance(name, str) else None
