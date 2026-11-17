"""Journalisation technique : logging standard, un format pour tout le
processus (API et worker).

Pas de structlog pour l'instant : aucun besoin d'export JSON ni de
corrélation avancée n'existe encore. Le jour où il apparaît, ce module est le
seul point à modifier.
"""

from __future__ import annotations

import logging.config


def configure_logging(level: str = "INFO") -> None:
    """Configure le logger racine. À appeler une fois au démarrage."""
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "format": (
                        "%(asctime)s level=%(levelname)s logger=%(name)s "
                        "msg=%(message)s"
                    ),
                },
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                },
            },
            "root": {"level": level, "handlers": ["console"]},
            # httpx journalise chaque requête en INFO : la sonde Qdrant de
            # /health/ready inonderait les logs.
            "loggers": {"httpx": {"level": "WARNING"}},
        }
    )
