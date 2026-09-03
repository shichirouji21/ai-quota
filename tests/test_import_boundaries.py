"""Enforce the layering: pure modules must not import side-effect modules."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PURE_MODULES = [
    "ai_quota/models.py",
    "ai_quota/timeutils.py",
    "ai_quota/ansi.py",
    "ai_quota/redact.py",
    "ai_quota/formatting.py",
]

FORBIDDEN_IMPORTS = {
    "subprocess", "pty", "socket", "urllib", "http",
    "ai_quota.providers", "ai_quota.providers.codex",
    "ai_quota.providers.copilot", "ai_quota.providers.claude",
    "ai_quota.coordinator", "ai_quota.cli", "ai_quota.cache",
}

ROOT = Path(__file__).resolve().parents[1] / "src"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize("relpath", PURE_MODULES)
def test_pure_module_has_no_side_effect_imports(relpath):
    path = ROOT / relpath
    if not path.exists():
        pytest.skip(f"{relpath} not present")
    got = _imports(path)
    offenders = got & FORBIDDEN_IMPORTS
    assert not offenders, f"{relpath} imports forbidden modules: {offenders}"
