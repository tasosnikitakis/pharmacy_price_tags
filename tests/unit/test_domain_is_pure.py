"""``core/domain`` must not import database, HTTP, file-system or process modules."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

DOMAIN = Path(__file__).resolve().parents[2] / "core" / "domain"
FORBIDDEN = {
    "sqlalchemy", "alembic", "psycopg", "fastapi", "starlette", "httpx", "requests",
    "urllib", "http", "socket", "asyncio", "subprocess", "os", "pathlib", "shutil",
    "io", "core.db", "core.app", "core.importers", "gateway", "bleak",
}  # fmt: skip


FORBIDDEN_CALLS = {"open", "print", "input", "exec", "eval", "__import__"}


def _calls(path: Path) -> set[str]:
    return {
        node.func.id
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize("path", sorted(DOMAIN.rglob("*.py")), ids=lambda p: p.name)
def test_no_io_imports(path: Path) -> None:
    for name in _imports(path):
        for bad in FORBIDDEN:
            assert name != bad and not name.startswith(bad + "."), f"{path.name} imports {name}"


@pytest.mark.parametrize("path", sorted(DOMAIN.rglob("*.py")), ids=lambda p: p.name)
def test_no_io_builtins(path: Path) -> None:
    assert not _calls(path) & FORBIDDEN_CALLS, path.name


def test_scan_found_files() -> None:
    assert len(list(DOMAIN.rglob("*.py"))) >= 3
