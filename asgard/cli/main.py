"""Console entry point."""

import argparse
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from asgard.cli.logs import show_logs
from asgard.core.config import ConfigError, RunConfig, prepare_run
from asgard.core.orchestrator import Orchestrator
from asgard.core.sinks import (
    CLIProgressSink,
    JSONLSink,
    LoggingSink,
    StreamJSONSink,
)
from asgard.core.stages.bundle import BundleStage, export_classifications
from asgard.core.stages.classify import ClassifyStage
from asgard.core.stages.consolidate import ConsolidateStage
from asgard.core.stages.mine import MineStage
from asgard.core.stages.normalize import NormalizeStage
from asgard.core.stages.security_md import SecurityMdStage
from asgard.core.stages.synthesize import SynthesizeStage
from asgard.providers.factory import LLMFactory

_ROLES = ("classifier", "evaluator", "synthesizer")
_PIPELINE = (
    "mine",
    "normalize",
    "classify",
    "consolidate",
    "synthesize",
    "security_md",
    "bundle",
)


def main(
    argv: list[str] | None = None,
    *,
    opener: Any = None,
    handlers: dict[str, Any] | None = None,
) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            export_classifications(args.run_dir)
            return
        if args.command == "logs":
            show_logs(args.run_dir, follow=args.follow, stage=args.stage)
            return
        if args.command != "run":
            parser.print_help()
            return
        config = prepare_run(_overrides(args), args.config, opener=opener)
        _run_pipeline(
            config,
            opener,
            handlers,
            start=args.start,
            only=args.only,
            quiet=args.quiet,
            json_logs=args.json_logs,
        )
    except (ConfigError, ValidationError, ValueError) as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(2) from None
    except Exception as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1) from None


def _run_pipeline(
    config: RunConfig,
    opener: Any,
    handlers: dict[str, Any] | None,
    *,
    start: str | None = None,
    only: str | None = None,
    quiet: bool = False,
    json_logs: bool = False,
) -> None:
    sinks: list[Any] = []
    if not quiet:
        sinks.append(CLIProgressSink())
    sinks.append(JSONLSink(config.output_dir / "events.jsonl"))
    if json_logs:
        sinks.append(StreamJSONSink(sys.stdout))
    sinks.append(LoggingSink())
    factory = LLMFactory(config.llm, handlers=handlers)
    stages = [
        MineStage(opener=opener),
        NormalizeStage(repo=config.repo),
        ClassifyStage(
            config.max_iterations,
            factory,
            batch_size=config.batch_size,
            batch_max_chars=config.batch_max_chars,
        ),
        ConsolidateStage(config.include_unresolved),
        SynthesizeStage(config.repo, factory),
        SecurityMdStage(config.repo, config.llm, path=config.security_md),
        BundleStage(config.export_xlsx),
    ]
    chosen = only if only is not None else start
    Orchestrator(stages, sinks).run(
        config.output_dir,
        only=only,
        start=start,
        data=config if chosen in (None, stages[0].name) else None,
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
        help=(
            "validate settings, save them, mine pull requests, normalize them, "
            "classify them, consolidate them, synthesize the guideline, "
            "write SECURITY.md, and write a run manifest"
        ),
    )
    run.add_argument("repo", nargs="?")
    span = run.add_mutually_exclusive_group()
    span.add_argument(
        "--from",
        dest="start",
        choices=_PIPELINE,
        help="start at this stage and continue; earlier stages must already be done",
    )
    span.add_argument(
        "--only",
        choices=_PIPELINE,
        help="run this stage alone; earlier stages must already be done",
    )
    run.add_argument("--config", type=Path)
    run.add_argument("--quiet", action="store_true", help="hide the progress bar")
    run.add_argument(
        "--json-logs",
        action="store_true",
        help="write one JSON event per line to stdout",
    )
    run.add_argument("--output-dir")
    run.add_argument("--security-md", type=Path)
    run.add_argument("--max-iterations", type=int)
    run.add_argument("--batch-size", type=int)
    run.add_argument("--batch-max-chars", type=int)
    run.add_argument("--prefilter", action=argparse.BooleanOptionalAction, default=None)
    run.add_argument("--include-unresolved", action=argparse.BooleanOptionalAction, default=None)
    run.add_argument("--export-xlsx", action=argparse.BooleanOptionalAction, default=None)
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
    export = sub.add_parser(
        "export",
        help="write a classification spreadsheet from a finished run",
    )
    export.add_argument("run_dir", type=Path)
    logs = sub.add_parser(
        "logs",
        help="print the progress log of a run, one stage at a time",
    )
    logs.add_argument("run_dir", type=Path)
    logs.add_argument(
        "--follow",
        action="store_true",
        help="keep reading new events until interrupted",
    )
    logs.add_argument("--stage", choices=_PIPELINE, help="show one stage")
    return parser


def _overrides(args: argparse.Namespace) -> dict:
    data: dict = {}
    if args.repo:
        data["repo"] = args.repo
    if args.output_dir:
        data["output_dir"] = args.output_dir
    if args.security_md is not None:
        data["security_md"] = args.security_md
    if args.max_iterations is not None:
        data["max_iterations"] = args.max_iterations
    if args.batch_size is not None:
        data["batch_size"] = args.batch_size
    if args.batch_max_chars is not None:
        data["batch_max_chars"] = args.batch_max_chars
    if args.prefilter is not None:
        data["prefilter"] = args.prefilter
    if args.include_unresolved is not None:
        data["include_unresolved"] = args.include_unresolved
    if args.export_xlsx is not None:
        data["export_xlsx"] = args.export_xlsx
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
