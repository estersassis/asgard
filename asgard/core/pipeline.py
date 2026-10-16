"""Build and run the standard ASGARD pipeline. Shared by CLI and UI."""

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from asgard.core.config import RunConfig
from asgard.core.orchestrator import Orchestrator
from asgard.core.sinks import JSONLSink, Sink
from asgard.core.stage import Stage
from asgard.core.stages.bundle import BundleStage
from asgard.core.stages.classify import ClassifyStage
from asgard.core.stages.consolidate import ConsolidateStage
from asgard.core.stages.mine import MineStage
from asgard.core.stages.normalize import NormalizeStage
from asgard.core.stages.prefilter import PrefilterStage
from asgard.core.stages.security_md import SecurityMdStage
from asgard.core.stages.synthesize import SynthesizeStage
from asgard.providers.factory import LLMFactory

PIPELINE = (
    "mine",
    "normalize",
    "prefilter",
    "classify",
    "consolidate",
    "synthesize",
    "security_md",
    "bundle",
)

_PROMPT = "system_prompt.txt"
_ROOT = {
    "security_md": ("SECURITY.md",),
    "bundle": ("manifest.json",),
}


def clear_from(run_dir: Path, stage: str) -> None:
    """Remove ``stage`` and every later stage output so resume can re-run them.

    Keeps ``synthesize/system_prompt.txt`` when that stage directory is cleared.
    Also drops root ``SECURITY.md`` / ``manifest.json`` when those stages are
    in the cleared range.
    """
    if stage not in PIPELINE:
        raise ValueError(f"unknown stage: {stage}")
    start = PIPELINE.index(stage)
    cleared = PIPELINE[start:]
    prompt_path = run_dir / "synthesize" / _PROMPT
    prompt: str | None = None
    if "synthesize" in cleared and prompt_path.is_file():
        prompt = prompt_path.read_text(encoding="utf-8")
    for name in cleared:
        directory = run_dir / name
        if directory.exists():
            shutil.rmtree(directory)
        for relative in _ROOT.get(name, ()):
            path = run_dir / relative
            if path.is_file():
                path.unlink()
    if prompt is not None:
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(prompt, encoding="utf-8")


def build_stages(
    config: RunConfig,
    *,
    opener: Any = None,
    handlers: dict[str, Any] | None = None,
) -> list[Stage]:
    factory = LLMFactory(config.llm, handlers=handlers)
    return [
        MineStage(opener=opener),
        NormalizeStage(repo=config.repo),
        PrefilterStage(),
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


def run_pipeline(
    config: RunConfig,
    *,
    start: str | None = None,
    only: str | None = None,
    sinks: list[Sink] | None = None,
    opener: Any = None,
    handlers: dict[str, Any] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> Any:
    """Run the pipeline into ``config.output_dir``, always writing ``events.jsonl``."""
    stages = build_stages(config, opener=opener, handlers=handlers)
    all_sinks: list[Sink] = list(sinks or [])
    all_sinks.append(JSONLSink(config.output_dir / "events.jsonl"))
    chosen = only if only is not None else start
    return Orchestrator(stages, all_sinks).run(
        config.output_dir,
        only=only,
        start=start,
        data=config if chosen in (None, stages[0].name) else None,
        cancel=cancel,
    )
