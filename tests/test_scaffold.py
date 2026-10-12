"""Skeleton checks for the package layout."""

import ast
import subprocess
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "asgard"
CORE_ROOT = PACKAGE_ROOT / "core"


def test_asgard_help_exits_zero() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "asgard.cli.main", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "asgard" in result.stdout


def test_core_does_not_import_interfaces() -> None:
    forbidden = ("asgard.cli", "asgard.ui")
    for path in CORE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                assert not any(name == item or name.startswith(f"{item}.") for item in forbidden)
