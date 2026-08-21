"""Architectural fitness test: the domain knows nothing about the outside world.

Clean architecture's dependency rule is only real if something enforces it. Rather than
adding import-linter and a CI job to a 24-hour build, one test walks the domain's ASTs
and fails the suite the moment a WebSocket, a Flask object, or an application-layer
import appears where pure logic belongs.

This is what keeps the domain unit-testable with no sockets, no event loop, and no
fixtures -- the property that made every other test in this directory fast and
deterministic.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.support import BACKEND_DIR

DOMAIN_DIR = BACKEND_DIR / "domain"
APPLICATION_DIR = BACKEND_DIR / "application"

# numpy for per-sample work, xxhash for the required digest. Nothing else: no web
# framework, no socket library, no serialisation format, no clock wrapper.
ALLOWED_THIRD_PARTY = {"numpy", "xxhash"}

FORBIDDEN_PREFIXES = (
    "flask",
    "flask_sock",
    "simple_websocket",
    "websockets",
    "gunicorn",
    "backend.application",
    "backend.infrastructure",
)

# The application layer orchestrates; it may know the domain and its own ports, and
# nothing outside. It is allowed no third-party imports the domain is not allowed,
# which is what forces serialisation and sockets to arrive as injected dependencies
# rather than as imports.
APPLICATION_FORBIDDEN_PREFIXES = (
    "flask",
    "flask_sock",
    "simple_websocket",
    "websockets",
    "gunicorn",
    "backend.infrastructure",
)


def domain_modules() -> list[Path]:
    return sorted(p for p in DOMAIN_DIR.glob("*.py") if p.name != "__init__.py")


def application_modules() -> list[Path]:
    return sorted(p for p in APPLICATION_DIR.glob("*.py") if p.name != "__init__.py")


def imported_roots(source: str) -> set[str]:
    """Top-level package name of every import in a module."""
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import, stays inside the domain
                continue
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


def imported_full_names(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module)
    return names


def test_the_domain_package_is_not_empty():
    """Guards against this suite passing vacuously if the directory is ever moved."""
    assert len(domain_modules()) >= 6


@pytest.mark.parametrize("module", domain_modules(), ids=lambda p: p.name)
def test_domain_has_no_infrastructure_imports(module: Path):
    for name in imported_full_names(module.read_text(encoding="utf-8")):
        assert not name.startswith(FORBIDDEN_PREFIXES), (
            f"{module.name} imports {name!r}: infrastructure has leaked into the domain"
        )


@pytest.mark.parametrize("module", domain_modules(), ids=lambda p: p.name)
def test_domain_third_party_imports_are_on_the_allowlist(module: Path):
    import sys

    stdlib = sys.stdlib_module_names
    for root in imported_roots(module.read_text(encoding="utf-8")):
        if root in stdlib or root == "backend":
            continue
        assert root in ALLOWED_THIRD_PARTY, (
            f"{module.name} imports third-party {root!r}, which is not on the domain allowlist"
        )


def test_the_application_package_is_not_empty():
    assert len(application_modules()) >= 3


@pytest.mark.parametrize("module", application_modules(), ids=lambda p: p.name)
def test_application_has_no_infrastructure_imports(module: Path):
    """The orchestration layer must not know that the wire is JSON or that the socket
    is flask-sock. This test is what forces ``MessageCodec`` and ``DownstreamSink`` to
    exist as ports -- delete it and importing ``wire`` directly would be the path of
    least resistance."""
    for name in imported_full_names(module.read_text(encoding="utf-8")):
        assert not name.startswith(APPLICATION_FORBIDDEN_PREFIXES), (
            f"{module.name} imports {name!r}: infrastructure has leaked into the application layer"
        )


@pytest.mark.parametrize("module", application_modules(), ids=lambda p: p.name)
def test_application_third_party_imports_are_on_the_allowlist(module: Path):
    import sys

    stdlib = sys.stdlib_module_names
    for root in imported_roots(module.read_text(encoding="utf-8")):
        if root in stdlib or root == "backend":
            continue
        assert root in ALLOWED_THIRD_PARTY, (
            f"{module.name} imports third-party {root!r}, "
            "which is not on the application allowlist"
        )
