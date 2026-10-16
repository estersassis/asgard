"""Tie a finished run together in a manifest."""

import hashlib
import json
import subprocess
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from asgard.core.config import ConfigError
from asgard.core.stage import RunContext, Stage
from asgard.prompts.classifier import SYSTEM_PROMPT as CLASSIFIER_PROMPT
from asgard.prompts.evaluator import SYSTEM_PROMPT as EVALUATOR_PROMPT
from asgard.prompts.synthesizer import SYSTEM_PROMPT as SYNTHESIZER_PROMPT
from asgard.providers.models import LLMSettings

_ROLES = ("classifier", "evaluator", "synthesizer")
_PIPELINE = (
    "mine",
    "normalize",
    "prefilter",
    "classify",
    "consolidate",
    "synthesize",
    "security_md",
    "bundle",
)
_SECRET_KEYS = ("github_token", "keys")
_LIST_OUTPUTS = ("mine", "normalize", "prefilter", "classify", "consolidate")
_FILES = {
    "config.json": "Redacted run settings.",
    "events.jsonl": "Progress events for the run.",
    "SECURITY.md": "Security guideline with pull request links and a provenance footer.",
    "manifest.json": "Run manifest.",
    "mine/output.json": "Mined pull request numbers.",
    "mine/metrics.json": "GraphQL mining request and cost metrics.",
    "normalize/output.json": "Normalized pull requests.",
    "normalize/items.jsonl": "Per-pull-request checkpoint for normalization.",
    "prefilter/output.json": "Pull requests kept for classification.",
    "prefilter/removed.json": "Pre-filter counts and the removed pull requests.",
    "classify/output.json": "Classification summary for each pull request.",
    "classify/items.jsonl": "Per-pull-request checkpoint for classification.",
    "classify/trace.jsonl": "One trace line per model call.",
    "classify/metrics.json": "Classifier-evaluator loop metrics.",
    "consolidate/output.json": "Security pull requests sent to synthesis.",
    "consolidate/audit.jsonl": "One audit record per pull request.",
    "consolidate/summary.json": "Security, non-security, and unresolved counts.",
    "synthesize/output.json": "Synthesized user guideline.",
    "security_md/output.json": "Path of SECURITY.md and citation warnings.",
    "bundle/output.json": "Stage output, the same document as manifest.json.",
    "bundle/classifications.xlsx": "Classification audit spreadsheet.",
}
_CACHED = "Cached GitHub pull request."
_UNKNOWN = "unrecognized run file"
_XLSX = (
    "pr_id",
    "status",
    "iterations",
    "decision",
    "rationale",
    "evidence",
    "verdict",
    "Evaluation",
    "Justify",
)
_OPENPYXL = "openpyxl is required to export the classification spreadsheet"


class BundleStage(Stage):
    name = "bundle"

    def __init__(self, export_xlsx: bool = False) -> None:
        self.export_xlsx = export_xlsx

    def execute(self, ctx: RunContext, data: Any) -> dict[str, Any]:
        config = _config(ctx)
        if self.export_xlsx:
            export_classifications(ctx.run_dir)
        manifest = {
            "version": _package_version(),
            "commit": _commit(),
            "config": config,
            "prompts": _prompts(config),
            "models": _models(config),
            "counts": _counts(ctx),
            "stages": _stages(ctx),
            "tokens": _tokens(ctx),
            "cost": None,
            "loop": _loop(ctx),
            "citation_warnings": _warnings(ctx),
            "files": _files(ctx),
        }
        _replace(ctx.run_dir / "manifest.json", json.dumps(manifest))
        return manifest


def _config(ctx: RunContext) -> dict[str, Any] | None:
    path = ctx.run_dir / "config.json"
    if not path.is_file():
        return None
    data = _read(path, "run configuration is invalid")
    if not isinstance(data, dict):
        raise ConfigError("run configuration is invalid")
    return {key: value for key, value in data.items() if key not in _SECRET_KEYS}


def _prompts(config: dict[str, Any] | None) -> dict[str, str | None]:
    repo = config.get("repo") if config else None
    synthesizer = None
    if isinstance(repo, str) and repo:
        synthesizer = _sha(SYNTHESIZER_PROMPT.format(repository=repo))
    return {
        "classifier": _sha(CLASSIFIER_PROMPT),
        "evaluator": _sha(EVALUATOR_PROMPT),
        "synthesizer": synthesizer,
    }


def _models(config: dict[str, Any] | None) -> dict[str, dict[str, str]] | None:
    if config is None:
        return None
    try:
        settings = LLMSettings.model_validate(config.get("llm"))
    except ValidationError:
        raise ConfigError("run configuration is invalid") from None
    models: dict[str, dict[str, str]] = {}
    for role in _ROLES:
        spec = settings.resolve(role)
        models[role] = {"provider": spec.provider, "model": spec.model}
    return models


def _counts(ctx: RunContext) -> dict[str, int | None]:
    summary = _summary(ctx)
    entered, kept = _prefilter_counts(ctx)
    return {
        "mined": _list_length(ctx, "mine"),
        "filtered": _filtered(ctx),
        "prefilter_in": entered,
        "prefilter_out": kept,
        "security": summary.get("security"),
        "non_security": summary.get("non_security"),
        "unresolved": summary.get("unresolved"),
    }


def _prefilter_counts(ctx: RunContext) -> tuple[int | None, int | None]:
    path = ctx.run_dir / "prefilter" / "removed.json"
    if not path.is_file():
        return None, None
    data = _read(path, "prefilter counts are invalid")
    if not isinstance(data, dict):
        raise ConfigError("prefilter counts are invalid")
    return _count(data.get("before")), _count(data.get("after"))


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError("prefilter counts are invalid")
    return value


def _summary(ctx: RunContext) -> dict[str, int | None]:
    path = ctx.run_dir / "consolidate" / "summary.json"
    if not path.is_file():
        return {"security": None, "non_security": None, "unresolved": None}
    data = _read(path, "consolidation summary is invalid")
    if not isinstance(data, dict):
        raise ConfigError("consolidation summary is invalid")
    counts: dict[str, int | None] = {}
    for key in ("security", "non_security", "unresolved"):
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError("consolidation summary is invalid")
        counts[key] = value
    return counts


def _filtered(ctx: RunContext) -> int:
    return sum(1 for event in _events(ctx) if event.get("type") == "PRFiltered")


def _stages(ctx: RunContext) -> dict[str, dict[str, Any]]:
    timings = {name: {"duration_s": None, "skipped": None} for name in _PIPELINE}
    open_at: dict[str, str | None] = {}
    for event in _events(ctx):
        stage = event.get("stage")
        if stage not in timings or not isinstance(stage, str):
            continue
        kind = event.get("type")
        if kind == "StageStarted":
            at = event.get("at")
            open_at[stage] = at if isinstance(at, str) else None
        elif kind == "StageFinished":
            skipped = event.get("skipped") is True
            if stage in open_at:
                start = open_at.pop(stage)
                finish = event.get("at")
                if isinstance(start, str) and isinstance(finish, str):
                    timings[stage]["duration_s"] = _seconds(start, finish)
                elif not skipped:
                    timings[stage]["duration_s"] = None
            timings[stage]["skipped"] = skipped
    stages: dict[str, dict[str, Any]] = {}
    for name in _PIPELINE:
        stages[name] = {
            "items": _items(ctx, name),
            "duration_s": timings[name]["duration_s"],
            "skipped": timings[name]["skipped"],
        }
    return stages


def _items(ctx: RunContext, name: str) -> int | None:
    if name in _LIST_OUTPUTS:
        return _list_length(ctx, name)
    if name == "synthesize":
        path = ctx.run_dir / name / "output.json"
        if not path.is_file():
            return None
        text = _read(path, "synthesized guideline is invalid")
        if not isinstance(text, str):
            raise ConfigError("synthesized guideline is invalid")
        return 1 if text else 0
    if name in ("security_md", "bundle"):
        if name == "bundle" or (ctx.run_dir / name / "output.json").is_file():
            return 1
        return None
    return None


def _list_length(ctx: RunContext, name: str) -> int | None:
    path = ctx.run_dir / name / "output.json"
    if not path.is_file():
        return None
    data = _read(path, f"{name} output is invalid")
    if not isinstance(data, list):
        raise ConfigError(f"{name} output is invalid")
    return len(data)


def _tokens(ctx: RunContext) -> dict[str, int] | None:
    path = ctx.run_dir / "classify" / "trace.jsonl"
    if not path.is_file():
        return None
    incoming = 0
    outgoing = 0
    for line in _lines(path, "classify trace is invalid"):
        if not isinstance(line, dict):
            raise ConfigError("classify trace is invalid")
        incoming += _token(line.get("input_tokens"))
        outgoing += _token(line.get("output_tokens"))
    return {"input": incoming, "output": outgoing}


def _token(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _loop(ctx: RunContext) -> dict[str, Any] | None:
    path = ctx.run_dir / "classify" / "metrics.json"
    if not path.is_file():
        return None
    data = _read(path, "loop metrics are invalid")
    if not isinstance(data, dict):
        raise ConfigError("loop metrics are invalid")
    return data


def _warnings(ctx: RunContext) -> list[str] | None:
    path = ctx.run_dir / "security_md" / "output.json"
    if not path.is_file():
        return None
    data = _read(path, "citation warnings are invalid")
    if not isinstance(data, dict):
        raise ConfigError("citation warnings are invalid")
    warnings = data.get("unknown_citations")
    if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
        raise ConfigError("citation warnings are invalid")
    return warnings


def _files(ctx: RunContext) -> dict[str, str]:
    found: dict[str, str] = {}
    for path in ctx.run_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ctx.run_dir).as_posix()
        found[rel] = _describe(rel)
    for rel in ("manifest.json", "bundle/output.json"):
        found.setdefault(rel, _describe(rel))
    return dict(sorted(found.items()))


def _describe(rel: str) -> str:
    if rel in _FILES:
        return _FILES[rel]
    name = rel.removeprefix("mine/prs/")
    cached = rel.startswith("mine/prs/") and name.endswith(".json")
    if cached and "/" not in name and name != ".json":
        return _CACHED
    return _UNKNOWN


def _events(ctx: RunContext) -> list[dict[str, Any]]:
    path = ctx.run_dir / "events.jsonl"
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in _lines(path, "progress events are invalid"):
        if not isinstance(line, dict):
            raise ConfigError("progress events are invalid")
        events.append(line)
    return events


def export_classifications(run_dir: Path) -> Path:
    """Write ``bundle/classifications.xlsx`` from ``classify/output.json``."""
    try:
        from openpyxl import Workbook
    except ImportError:
        raise ConfigError(_OPENPYXL) from None
    path = run_dir / "classify" / "output.json"
    data = _read(path, "classify output is invalid") if path.is_file() else None
    if not isinstance(data, list):
        raise ConfigError("classify output is invalid")
    book = Workbook()
    sheet = book.active
    sheet.title = "classifications"
    sheet.append(list(_XLSX))
    for item in data:
        sheet.append(_row(item))
    destination = run_dir / "bundle" / "classifications.xlsx"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.tmp.xlsx")
    book.save(temporary)
    temporary.replace(destination)
    return destination


def _row(item: Any) -> list[Any]:
    if not isinstance(item, dict):
        raise ConfigError("classify output is invalid")
    classification = item.get("classification")
    evaluation = item.get("evaluation")
    decision = None
    rationale = ""
    evidence = ""
    if isinstance(classification, dict):
        value = classification.get("has_security_discussion")
        if isinstance(value, bool):
            decision = value
        text = classification.get("rationale")
        if isinstance(text, str):
            rationale = text
        quotes = classification.get("evidence")
        if isinstance(quotes, list):
            evidence = json.dumps(quotes)
    verdict = ""
    if isinstance(evaluation, dict) and isinstance(evaluation.get("verdict"), str):
        verdict = evaluation["verdict"]
    return [
        item.get("pr_id"),
        item.get("status"),
        item.get("iterations"),
        decision,
        rationale,
        evidence,
        verdict,
        "",
        "",
    ]


def _seconds(start: str, finish: str) -> float | None:
    try:
        opened = datetime.fromisoformat(start)
        closed = datetime.fromisoformat(finish)
    except ValueError:
        return None
    return (closed - opened).total_seconds()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _package_version() -> str:
    try:
        return version("asgard")
    except PackageNotFoundError:
        return "0.1.0"


def _commit() -> str | None:
    root = _checkout()
    if root is None:
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    commit = result.stdout.strip()
    if len(commit) != 40:
        return None
    return commit


def _checkout() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        if (parent / ".git").exists():
            return parent
    return None


def _read(path: Path, message: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ConfigError(message) from None


def _lines(path: Path, message: str) -> list[Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise ConfigError(message) from None
    lines: list[Any] = []
    for line in text.splitlines():
        if not line:
            continue
        try:
            lines.append(json.loads(line))
        except json.JSONDecodeError:
            raise ConfigError(message) from None
    return lines


def _replace(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
