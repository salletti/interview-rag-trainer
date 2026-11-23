"""``knowledge_base.register(app, resources)`` monté par ``runtime.app`` (S2).

Les deux routes d'import existent dès la construction de l'application, sous
le préfixe ``/api/v1/knowledge-base``, en ``POST`` uniquement.
"""

from __future__ import annotations

from fastapi.routing import APIRoute

from runtime.app import create_app
from tests.support import unreachable_settings

PREFIX = "/api/v1/knowledge-base"


def test_create_app_mounts_the_import_routes() -> None:
    app = create_app(unreachable_settings())

    routes = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in (route.methods or set())
    }

    assert ("POST", f"{PREFIX}/documents") in routes
    assert ("POST", f"{PREFIX}/documents/{{document_id}}/versions") in routes


def test_the_import_routes_are_documented_in_the_openapi_schema() -> None:
    schema = create_app(unreachable_settings()).openapi()

    assert "post" in schema["paths"][f"{PREFIX}/documents"]
    assert "post" in schema["paths"][f"{PREFIX}/documents/{{document_id}}/versions"]
