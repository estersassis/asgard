"""Pilot runner: reproducibility at temperature 0 and single-pass path."""

from __future__ import annotations

import json
from pathlib import Path

from asgard.core.pilot.runner import run_pilot
from asgard.core.pilot.set import PilotSet, load_pilot_set
from asgard.core.prompt_registry import PromptRegistry
from asgard.providers.factory import LLMFactory
from asgard.providers.models import Credentials, LLMResponse, LLMSettings, Message, RoleSpec

_CRITERIA = ("decision", "evidence", "completeness", "rationale")


class _RecordingFake:
    """Deterministic fake that records temperature and never needs a script."""

    def __init__(self) -> None:
        self.temperatures: list[float] = []
        self.calls: list[list[Message]] = []
        self.evaluator_calls = 0
        self.classifier_calls = 0

    def generate(
        self,
        messages: list[Message],
        *,
        schema: type | None = None,
        temperature: float = 0,
        timeout: float = 300,
    ) -> LLMResponse:
        self.calls.append(list(messages))
        self.temperatures.append(temperature)
        system = messages[0].content if messages else ""
        if system.startswith("You review"):
            self.evaluator_calls += 1
            return LLMResponse(
                text=_approve(),
                input_tokens=3,
                output_tokens=5,
                latency_s=0.01,
                model="fake",
            )
        self.classifier_calls += 1
        security = _wants_security(messages)
        return LLMResponse(
            text=_classification(security, messages),
            input_tokens=3,
            output_tokens=5,
            latency_s=0.01,
            model="fake",
        )


def _wants_security(messages: list[Message]) -> bool:
    blob = "\n".join(message.content for message in messages).lower()
    return "authentication bypass" in blob or "xss" in blob


def _quote_from(messages: list[Message]) -> str:
    blob = "\n".join(message.content for message in messages)
    for needle in ("authentication bypass", "stored XSS", "XSS"):
        if needle in blob:
            return needle
    return "authentication bypass"


def _classification(security: bool, messages: list[Message]) -> str:
    if security:
        body: dict[str, object] = {
            "has_security_discussion": True,
            "evidence": [{"quote": _quote_from(messages), "location": "description"}],
            "rationale": "security discussion present",
        }
    else:
        body = {
            "has_security_discussion": False,
            "evidence": [],
            "rationale": "no security discussion",
        }
    return json.dumps(body)


def _approve() -> str:
    return json.dumps(
        {
            "verdict": "APPROVE",
            "findings": [
                {
                    "criterion": criterion,
                    "passed": True,
                    "explanation": f"{criterion} ok",
                }
                for criterion in _CRITERIA
            ],
            "feedback": "",
        }
    )


def _factory(handler: object, *, temperature: float = 0.7) -> LLMFactory:
    settings = LLMSettings(
        default=RoleSpec(provider="fake", model="fake", temperature=temperature),
        classifier=RoleSpec(provider="fake", model="class", temperature=temperature),
        evaluator=RoleSpec(provider="fake", model="eval", temperature=temperature),
    )
    return LLMFactory(
        settings,
        Credentials(),
        handlers={"fake": handler},
        sleep=lambda _: None,
    )


def _registry(tmp_path: Path) -> PromptRegistry:
    registry = PromptRegistry(tmp_path / "prompts")
    registry.save(
        "classifier",
        name="active-pilot",
        body="You decide whether one pull request contains a security discussion.\nACTIVE",
    )
    registry.save(
        "classifier",
        name="candidate-pilot",
        body="You decide whether one pull request contains a security discussion.\nCANDIDATE",
    )
    return registry


def test_run_pilot_reproducible_and_forces_temperature_zero(tmp_path: Path) -> None:
    pilot = load_pilot_set("example")
    registry = _registry(tmp_path)
    versions = [v.id for v in registry.list("classifier") if v.id != "default"]
    active_id = next(v for v in versions if v.startswith("active-pilot"))
    candidate_id = next(v for v in versions if v.startswith("candidate-pilot"))

    first_handler = _RecordingFake()
    first = run_pilot(
        pilot,
        candidate=candidate_id,
        active=active_id,
        factory=_factory(first_handler, temperature=0.7),
        registry_dir=registry.root,
        output_dir=tmp_path / "run-a",
    )

    second_handler = _RecordingFake()
    second = run_pilot(
        pilot,
        candidate=candidate_id,
        active=active_id,
        factory=_factory(second_handler, temperature=0.7),
        registry_dir=registry.root,
        output_dir=tmp_path / "run-b",
    )

    assert first.active == second.active
    assert first.candidate == second.candidate
    assert first.candidate_single_pass == second.candidate_single_pass
    assert first.confusion_candidate == second.confusion_candidate
    assert first.predictions == second.predictions
    assert first_handler.temperatures
    assert all(temp == 0.0 for temp in first_handler.temperatures)
    assert all(temp == 0.0 for temp in second_handler.temperatures)

    metrics = json.loads((tmp_path / "run-a" / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["candidate"]["f1"] == first.candidate.f1
    pred_lines = (tmp_path / "run-a" / "predictions.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(pred_lines) == len(pilot.items)


def test_single_pass_skips_evaluator(tmp_path: Path) -> None:
    pilot = load_pilot_set("example")
    # Two items only so the call counts stay easy to reason about.
    slim = PilotSet(
        id="slim",
        name="slim",
        items=[pilot.items[0], pilot.items[2]],
    )
    registry = _registry(tmp_path)
    versions = [v.id for v in registry.list("classifier") if v.id != "default"]
    candidate_id = next(v for v in versions if v.startswith("candidate-pilot"))

    handler = _RecordingFake()
    result = run_pilot(
        slim,
        candidate=candidate_id,
        active="default",
        factory=_factory(handler),
        registry_dir=registry.root,
        output_dir=tmp_path / "single",
        max_iterations=1,
    )

    n = len(slim.items)
    # active + candidate full loops each do classify+evaluate per PR; single-pass
    # adds one classify per PR and never calls the evaluator.
    assert handler.classifier_calls == n * 3
    assert handler.evaluator_calls == n * 2
    assert result.mode == "full"
    assert result.candidate_single_pass.confusion.n == n
    assert (tmp_path / "single" / "candidate" / "classify" / "output.json").is_file()


def test_classifier_mode_reports_progress(tmp_path: Path) -> None:
    pilot = load_pilot_set("example")
    slim = PilotSet(
        id="slim",
        name="slim",
        items=[pilot.items[0], pilot.items[2]],
    )
    registry = _registry(tmp_path)
    versions = [v.id for v in registry.list("classifier") if v.id != "default"]
    candidate_id = next(v for v in versions if v.startswith("candidate-pilot"))

    updates: list[str] = []
    run_pilot(
        slim,
        candidate=candidate_id,
        active="default",
        factory=_factory(_RecordingFake()),
        registry_dir=registry.root,
        output_dir=tmp_path / "progress",
        mode="classifier",
        on_progress=lambda progress: updates.append(progress.message),
    )

    assert updates
    assert any("Active prompt" in message for message in updates)
    assert any("Candidate prompt" in message for message in updates)
    assert any("2/2 PRs" in message for message in updates)
    assert any("overall 4/4" in message for message in updates)
    assert not any("/5" in message for message in updates)


def test_classifier_mode_skips_full_loop(tmp_path: Path) -> None:
    pilot = load_pilot_set("example")
    slim = PilotSet(
        id="slim",
        name="slim",
        items=[pilot.items[0], pilot.items[2]],
    )
    registry = _registry(tmp_path)
    versions = [v.id for v in registry.list("classifier") if v.id != "default"]
    candidate_id = next(v for v in versions if v.startswith("candidate-pilot"))

    handler = _RecordingFake()
    result = run_pilot(
        slim,
        candidate=candidate_id,
        active="default",
        factory=_factory(handler),
        registry_dir=registry.root,
        output_dir=tmp_path / "classifier-only",
        mode="classifier",
    )

    n = len(slim.items)
    assert result.mode == "classifier"
    assert handler.classifier_calls == n * 2
    assert handler.evaluator_calls == 0
    assert result.candidate == result.candidate_single_pass
    assert not (tmp_path / "classifier-only" / "active").exists()
    assert not (tmp_path / "classifier-only" / "candidate").exists()


def test_unknown_candidate_raises(tmp_path: Path) -> None:
    registry = PromptRegistry(tmp_path / "prompts")
    try:
        run_pilot(
            "example",
            candidate="missing-prompt",
            factory=_factory(_RecordingFake()),
            registry_dir=registry.root,
            output_dir=tmp_path / "fail",
        )
    except LookupError as exc:
        assert "missing-prompt" in str(exc)
    else:
        raise AssertionError("expected LookupError")
