"""Console entry point."""

import argparse
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from asgard.cli.logs import show_logs
from asgard.cli.pilot import (
    bundled_pilot_set_help,
    promote_pilot_command,
    run_pilot_command,
)
from asgard.cli.prompts import list_prompts, save_prompt
from asgard.core.config import ConfigError, RunConfig, prepare_run
from asgard.core.pilot.gate import PromoteError
from asgard.core.pipeline import PIPELINE, run_pipeline
from asgard.core.sinks import CLIProgressSink, LoggingSink, StreamJSONSink
from asgard.core.stages.bundle import export_classifications

_ROLES = ("classifier", "evaluator", "synthesizer")
_PIPELINE = PIPELINE


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
        if args.command == "prompts":
            _run_prompts(args)
            return
        if args.command == "pilot":
            _run_pilot(args, handlers=handlers)
            return
        if args.command == "ui":
            try:
                from asgard.ui.app import serve
            except ImportError as exc:
                print(
                    "the web UI requires asgard[ui]; install with: pip install 'asgard[ui]'",
                    file=sys.stderr,
                )
                raise SystemExit(1) from exc
            serve(host=args.host, port=args.port)
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
    except (ConfigError, PromoteError, ValidationError, ValueError, LookupError) as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(2) from None
    except Exception as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1) from None


def _run_prompts(args: argparse.Namespace) -> None:
    if args.prompts_command == "list":
        list_prompts(role=args.role, registry_dir=args.registry_dir)
        return
    if args.prompts_command == "save":
        save_prompt(
            role=args.role,
            name=args.name,
            file=args.file,
            notes=args.notes or "",
            registry_dir=args.registry_dir,
        )
        return
    raise SystemExit(2)


def _run_pilot(args: argparse.Namespace, *, handlers: dict[str, Any] | None) -> None:
    if args.pilot_command == "run":
        run_pilot_command(
            candidate=args.candidate,
            active=args.active,
            pilot_set=args.pilot_set,
            config=args.config,
            f1_min=args.f1_min,
            kappa_min=args.kappa_min,
            accuracy_min=args.accuracy_min,
            precision_min=args.precision_min,
            recall_min=args.recall_min,
            output_dir=args.output_dir,
            registry_dir=args.registry_dir,
            handlers=handlers,
        )
        return
    if args.pilot_command == "promote":
        promote_pilot_command(
            candidate=args.candidate,
            config=args.config,
            from_result=args.from_result,
            force=args.force,
        )
        return
    raise SystemExit(2)


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
    if json_logs:
        sinks.append(StreamJSONSink(sys.stdout))
    sinks.append(LoggingSink())
    run_pipeline(
        config,
        start=start,
        only=only,
        sinks=sinks,
        opener=opener,
        handlers=handlers,
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
            "pre-filter them, classify them, consolidate them, synthesize the guideline, "
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
    ui = sub.add_parser("ui", help="open the web UI (requires asgard[ui])")
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--port", type=int, default=8000)
    prompts = sub.add_parser("prompts", help="list or save versioned system prompts")
    prompts_sub = prompts.add_subparsers(dest="prompts_command", required=True)
    prompts_list = prompts_sub.add_parser("list", help="list prompt versions")
    prompts_list.add_argument(
        "--role",
        choices=_ROLES,
        help="list one role; omit to list all",
    )
    prompts_list.add_argument(
        "--registry-dir",
        type=Path,
        help="prompt registry root (default: ASGARD_PROMPTS_DIR or ~/.asgard/prompts)",
    )
    prompts_save = prompts_sub.add_parser("save", help="save a prompt version from a file")
    prompts_save.add_argument("--role", required=True, choices=_ROLES)
    prompts_save.add_argument("--name", required=True, help="display name (id is slugified)")
    prompts_save.add_argument("--notes", default="", help="optional notes stored with the version")
    prompts_save.add_argument(
        "--file",
        type=Path,
        required=True,
        help="file whose contents become the prompt body",
    )
    prompts_save.add_argument(
        "--registry-dir",
        type=Path,
        help="prompt registry root (default: ASGARD_PROMPTS_DIR or ~/.asgard/prompts)",
    )
    pilot = sub.add_parser(
        "pilot",
        help="validate classifier prompts on a labeled pilot set and promote when gates pass",
    )
    pilot_sub = pilot.add_subparsers(dest="pilot_command", required=True)
    pilot_run = pilot_sub.add_parser(
        "run",
        help="compare active vs candidate on a pilot set (temperature forced to 0)",
    )
    pilot_run.add_argument("--candidate", required=True, help="candidate classifier prompt id")
    pilot_run.add_argument(
        "--active",
        help="active classifier prompt id (default: [prompts].classifier from config)",
    )
    pilot_run.add_argument(
        "--pilot-set",
        default="example",
        help=bundled_pilot_set_help(),
    )
    pilot_run.add_argument("--config", type=Path, help="run settings TOML/JSON (LLM + prompts)")
    pilot_run.add_argument("--f1-min", type=float, default=0.90)
    pilot_run.add_argument("--kappa-min", type=float, default=0.80)
    pilot_run.add_argument("--accuracy-min", type=float, default=None)
    pilot_run.add_argument("--precision-min", type=float, default=None)
    pilot_run.add_argument("--recall-min", type=float, default=None)
    pilot_run.add_argument(
        "--output-dir",
        type=Path,
        help="write metrics.json / predictions.jsonl here (default: runs/_pilot/<stamp>/)",
    )
    pilot_run.add_argument(
        "--registry-dir",
        type=Path,
        help="prompt registry root (default: config prompts.registry_dir or ASGARD_PROMPTS_DIR)",
    )
    pilot_promote = pilot_sub.add_parser(
        "promote",
        help="write [prompts].classifier when the last (or given) pilot run cleared gates",
    )
    pilot_promote.add_argument("--candidate", required=True, help="classifier prompt id to promote")
    pilot_promote.add_argument(
        "--config",
        type=Path,
        required=True,
        help="TOML settings file whose [prompts] table is updated",
    )
    pilot_promote.add_argument(
        "--from-result",
        type=Path,
        help="metrics.json from a pilot run (default: newest under runs/_pilot/)",
    )
    pilot_promote.add_argument(
        "--force",
        action="store_true",
        help="update config even when gates failed or no pilot result is available",
    )
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
