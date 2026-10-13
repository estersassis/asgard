"""Console entry point."""

import argparse
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from asgard.core.config import ConfigError, RunConfig, prepare_run
from asgard.core.orchestrator import Orchestrator
from asgard.core.sinks import CLIProgressSink, JSONLSink, LoggingSink
from asgard.core.stages.classify import ClassifyStage
from asgard.core.stages.mine import MineStage
from asgard.core.stages.normalize import NormalizeStage
from asgard.providers.factory import LLMFactory

_ROLES = ("classifier", "evaluator", "synthesizer")
_PIPELINE = ("mine", "normalize", "classify")


def main(
    argv: list[str] | None = None,
    *,
    opener: Any = None,
    handlers: dict[str, Any] | None = None,
) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command != "run":
        parser.print_help()
        return
    try:
        config = prepare_run(_overrides(args), args.config, opener=opener)
    except (ConfigError, ValidationError, ValueError) as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(2) from None
    try:
        _run_pipeline(config, opener, handlers, start=args.start)
    except (ConfigError, ValueError) as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(2) from None


def _run_pipeline(
    config: RunConfig,
    opener: Any,
    handlers: dict[str, Any] | None,
    *,
    start: str | None = None,
) -> None:
    sinks = [
        CLIProgressSink(),
        JSONLSink(config.output_dir / "events.jsonl"),
        LoggingSink(),
    ]
    factory = LLMFactory(config.llm, handlers=handlers)
    stages = [
        MineStage(opener=opener),
        NormalizeStage(repo=config.repo),
        ClassifyStage(config.max_iterations, factory),
    ]
    Orchestrator(stages, sinks).run(
        config.output_dir,
        start=start,
        data=config if start in (None, stages[0].name) else None,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="asgard",
        description=(
            "Generate evidence-based SECURITY.md files from GitHub pull request discussions."
        ),
    )
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser(
        "run",
        help="validate settings, save them, mine pull requests, normalize them, and classify them",
    )
    run.add_argument("repo", nargs="?")
    run.add_argument(
        "--from",
        dest="start",
        choices=_PIPELINE,
        help="start at this stage and continue; earlier stages must already be done",
    )
    run.add_argument("--config", type=Path)
    run.add_argument("--output-dir")
    run.add_argument("--max-iterations", type=int)
    run.add_argument("--prefilter", action=argparse.BooleanOptionalAction, default=None)
    run.add_argument("--state", choices=("open", "closed", "all"))
    run.add_argument("--merged-only", action=argparse.BooleanOptionalAction, default=None)
    run.add_argument("--since")
    run.add_argument("--until")
    run.add_argument("--max-prs", type=int)
    run.add_argument("--label", action="append")
    run.add_argument("--provider")
    run.add_argument("--model")
    for role in _ROLES:
        run.add_argument(f"--{role}-provider")
        run.add_argument(f"--{role}-model")
    return parser


def _overrides(args: argparse.Namespace) -> dict:
    data: dict = {}
    if args.repo:
        data["repo"] = args.repo
    if args.output_dir:
        data["output_dir"] = args.output_dir
    if args.max_iterations is not None:
        data["max_iterations"] = args.max_iterations
    if args.prefilter is not None:
        data["prefilter"] = args.prefilter
    filters: dict = {}
    if args.state:
        filters["state"] = args.state
    if args.merged_only is not None:
        filters["merged_only"] = args.merged_only
    if args.since:
        filters["since"] = args.since
    if args.until:
        filters["until"] = args.until
    if args.max_prs is not None:
        filters["max_prs"] = args.max_prs
    if args.label:
        filters["labels"] = args.label
    if filters:
        data["filters"] = filters
    llm: dict = {}
    default: dict = {}
    if args.provider:
        default["provider"] = args.provider
    if args.model:
        default["model"] = args.model
    if default:
        llm["default"] = default
    for role in _ROLES:
        spec: dict = {}
        if provider := getattr(args, f"{role}_provider"):
            spec["provider"] = provider
        if model := getattr(args, f"{role}_model"):
            spec["model"] = model
        if spec:
            llm[role] = spec
    if llm:
        data["llm"] = llm
    return data


if __name__ == "__main__":
    main()
