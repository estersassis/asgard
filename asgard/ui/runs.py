"""Discover and identify on-disk runs for the UI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_DEFAULT_ROOTS = (Path("runs"), Path("tmp/runs"))


def run_roots() -> tuple[Path, ...]:
    return _DEFAULT_ROOTS


def stable_run_id(output_dir: Path, roots: tuple[Path, ...] | None = None) -> str | None:
    """Return ``{repo}--{stamp}`` when ``output_dir`` sits under a known runs root."""
    resolved = output_dir.resolve()
    for root in roots or run_roots():
        root = root.resolve()
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            continue
        parts = relative.parts
        if len(parts) == 2 and parts[0] and parts[1]:
            return f"{parts[0]}--{parts[1]}"
    return None


def resolve_run_dir(run_id: str, roots: tuple[Path, ...] | None = None) -> Path | None:
    """Map a stable run id back to its directory, if it contains config.json."""
    if "--" not in run_id or "/" in run_id or "\\" in run_id:
        return None
    repo, stamp = run_id.split("--", 1)
    if not repo or not stamp or "--" in stamp:
        return None
    if ".." in repo or ".." in stamp:
        return None
    for root in roots or run_roots():
        path = (root / repo / stamp).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError:
            continue
        if (path / "config.json").is_file():
            return path
    return None


def resolve_config_path(path: Path | str, roots: tuple[Path, ...] | None = None) -> Path:
    """Resolve a settings path, including mistaken ``runs/{id}/config.json`` forms.

    Accepts a real file path, or a path whose parent (or sole component) is a
    stable run id ``{repo}--{stamp}`` — mapped via :func:`resolve_run_dir`.
    """
    candidate = Path(path)
    if candidate.is_file():
        return candidate

    run_id: str | None = None
    if candidate.name == "config.json":
        run_id = candidate.parent.name
    elif candidate.suffix == "" and "--" in candidate.name and len(candidate.parts) == 1:
        run_id = candidate.name

    if run_id:
        run_dir = resolve_run_dir(run_id, roots=roots)
        if run_dir is not None:
            return run_dir / "config.json"

    return candidate


def discover_runs(roots: tuple[Path, ...] | None = None) -> list[dict[str, Any]]:
    """List run directories under the known roots, newest first."""
    found: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for root in roots or run_roots():
        if not root.is_dir():
            continue
        root = root.resolve()
        for config in root.glob("*/*/config.json"):
            run_dir = config.parent.resolve()
            if run_dir in seen:
                continue
            run_id = stable_run_id(run_dir, (root,))
            if run_id is None:
                continue
            seen.add(run_dir)
            repo = _repo(config)
            mtime = config.stat().st_mtime
            found.append(
                {
                    "id": run_id,
                    "output_dir": str(run_dir),
                    "repo": repo,
                    "stamp": run_dir.name,
                    "complete": (run_dir / "SECURITY.md").is_file()
                    or (run_dir / "bundle" / "output.json").is_file(),
                    "mtime": mtime,
                }
            )
    found.sort(key=lambda item: item["mtime"], reverse=True)
    for item in found:
        item.pop("mtime", None)
    return found


def _repo(config_path: Path) -> str:
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return config_path.parent.parent.name.replace("__", "/", 1)
    if isinstance(data, dict) and isinstance(data.get("repo"), str) and data["repo"]:
        return data["repo"]
    return config_path.parent.parent.name.replace("__", "/", 1)
