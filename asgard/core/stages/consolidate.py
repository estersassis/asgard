"""Select security discussions and record what happened to each pull request."""

import json
from pathlib import Path
from typing import Any

from asgard.core.config import ConfigError
from asgard.core.stage import RunContext, Stage

_TRACE = "classify/trace.jsonl"
_CLASSIFIED = "consolidate stage requires classified pull requests"


class ConsolidateStage(Stage):
    name = "consolidate"

    def __init__(self, include_unresolved: bool = False) -> None:
        self.include_unresolved = include_unresolved

    def execute(self, ctx: RunContext, data: Any) -> list[dict[str, Any]]:
        classified = _classified(data)
        normalized = _normalized(ctx)
        lines = _trace_lines(ctx)
        _check_mine(ctx, [item["pr_id"] for item in classified])

        synthesis: list[dict[str, Any]] = []
        audit: list[dict[str, Any]] = []
        security = 0
        non_security = 0
        unresolved = 0
        for item in classified:
            pr_id = item["pr_id"]
            status = item["status"]
            decision = item["decision"]
            source = normalized.get(pr_id)
            if source is None:
                raise ConfigError(f"normalized pull {pr_id} is missing")
            bucket = _bucket(status, decision)
            if bucket == "security":
                security += 1
            elif bucket == "non_security":
                non_security += 1
            else:
                unresolved += 1
            audit.append(
                {
                    "pr_id": pr_id,
                    "decision": decision,
                    "status": status,
                    "iterations": item["iterations"],
                    "trace": _TRACE,
                    "trace_lines": lines.get(pr_id, []),
                }
            )
            if _for_synthesis(status, decision, self.include_unresolved):
                synthesis.append(_synthesis(pr_id, source, item["classification"]))

        directory = ctx.run_dir / self.name
        body = "".join(json.dumps(record) + "\n" for record in audit)
        _replace(directory / "audit.jsonl", body)
        summary = {
            "total": len(classified),
            "security": security,
            "non_security": non_security,
            "unresolved": unresolved,
        }
        _replace(directory / "summary.json", json.dumps(summary) + "\n")
        return synthesis


def _classified(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, list):
        raise ConfigError(_CLASSIFIED)
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for raw in data:
        item = _item(raw)
        pr_id = item["pr_id"]
        if pr_id in seen:
            raise ConfigError(f"duplicate pull request {pr_id}")
        seen.add(pr_id)
        items.append(item)
    return items


def _item(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or "classification" not in raw:
        raise ConfigError(_CLASSIFIED)
    pr_id = raw.get("pr_id")
    status = raw.get("status")
    iterations = raw.get("iterations")
    classification = raw.get("classification")
    if not isinstance(pr_id, str) or not pr_id:
        raise ConfigError(_CLASSIFIED)
    if not isinstance(status, str) or not status:
        raise ConfigError(_CLASSIFIED)
    if isinstance(iterations, bool) or not isinstance(iterations, int) or iterations < 0:
        raise ConfigError(_CLASSIFIED)
    if classification is not None and not isinstance(classification, dict):
        raise ConfigError(_CLASSIFIED)
    return {
        "pr_id": pr_id,
        "status": status,
        "iterations": iterations,
        "classification": classification,
        "decision": _decision(classification),
    }


def _decision(classification: dict[str, Any] | None) -> bool | None:
    if classification is None:
        return None
    value = classification.get("has_security_discussion")
    if not isinstance(value, bool):
        raise ConfigError(_CLASSIFIED)
    return value


def _normalized(ctx: RunContext) -> dict[str, dict[str, str]]:
    path = ctx.run_dir / "normalize" / "output.json"
    if not path.is_file():
        raise ConfigError("normalized pull requests are missing")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise ConfigError("normalized pull requests are missing") from None
    if not isinstance(data, list):
        raise ConfigError("normalized pull requests are missing")
    index: dict[str, dict[str, str]] = {}
    for raw in data:
        pr_id, source = _source(raw)
        if pr_id in index:
            raise ConfigError(f"duplicate pull request {pr_id}")
        index[pr_id] = source
    return index


def _source(raw: Any) -> tuple[str, dict[str, str]]:
    if not isinstance(raw, dict):
        raise ConfigError("normalized pull requests are missing")
    context = raw.get("context")
    if not isinstance(context, dict):
        raise ConfigError("normalized pull requests are missing")
    pr_id = context.get("pr_id")
    title = context.get("title")
    repository = context.get("repository")
    if not isinstance(pr_id, str) or not pr_id:
        raise ConfigError("normalized pull requests are missing")
    if not isinstance(title, str) or not isinstance(repository, str):
        raise ConfigError("normalized pull requests are missing")
    return pr_id, {"title": title, "repository": repository}


def _trace_lines(ctx: RunContext) -> dict[str, list[int]]:
    path = ctx.run_dir / _TRACE
    if not path.is_file():
        raise ConfigError("classify trace is missing")
    lines: dict[str, list[int]] = {}
    text = path.read_text(encoding="utf-8")
    for number, line in enumerate(text.splitlines(), 1):
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            raise ConfigError("classify trace is not valid json") from None
        if not isinstance(payload, dict):
            raise ConfigError("classify trace is not valid json")
        pr_id = payload.get("pr_id")
        if not isinstance(pr_id, str):
            raise ConfigError("classify trace is not valid json")
        lines.setdefault(pr_id, []).append(number)
    return lines


def _check_mine(ctx: RunContext, pr_ids: list[str]) -> None:
    path = ctx.run_dir / "mine" / "output.json"
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise ConfigError("mined pull requests were not a list of numbers") from None
    if not isinstance(data, list) or any(not _number(item) for item in data):
        raise ConfigError("mined pull requests were not a list of numbers")
    if {str(number) for number in data} != set(pr_ids):
        raise ConfigError("classified pull requests do not match the mined set")


def _number(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _bucket(status: str, decision: bool | None) -> str:
    if status == "approved":
        if decision is True:
            return "security"
        if decision is False:
            return "non_security"
        raise ConfigError(_CLASSIFIED)
    if status == "error" or status.startswith("unresolved_"):
        return "unresolved"
    raise ConfigError(_CLASSIFIED)


def _for_synthesis(status: str, decision: bool | None, include_unresolved: bool) -> bool:
    if decision is not True:
        return False
    if status == "approved":
        return True
    return include_unresolved and (status == "error" or status.startswith("unresolved_"))


def _synthesis(pr_id: str, source: dict[str, str], classification: Any) -> dict[str, Any]:
    if not isinstance(classification, dict):
        raise ConfigError(_CLASSIFIED)
    repository = source["repository"]
    return {
        "pr_id": pr_id,
        "title": source["title"],
        "link": f"https://github.com/{repository}/pull/{pr_id}",
        "evidence": _evidence(classification),
        "rationale": _rationale(classification),
    }


def _evidence(classification: dict[str, Any]) -> list[dict[str, str]]:
    evidence = classification.get("evidence")
    if not isinstance(evidence, list):
        raise ConfigError(_CLASSIFIED)
    quotes: list[dict[str, str]] = []
    for item in evidence:
        if not isinstance(item, dict):
            raise ConfigError(_CLASSIFIED)
        quote = item.get("quote")
        location = item.get("location")
        if not isinstance(quote, str) or not isinstance(location, str):
            raise ConfigError(_CLASSIFIED)
        quotes.append({"quote": quote, "location": location})
    return quotes


def _rationale(classification: dict[str, Any]) -> str:
    rationale = classification.get("rationale")
    if not isinstance(rationale, str):
        raise ConfigError(_CLASSIFIED)
    return rationale


def _replace(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
