"""Classify each pull request in a bounded review loop."""

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from asgard.core.classification import Classification, classify
from asgard.core.config import ConfigError
from asgard.core.evaluation import Evaluation, evaluate
from asgard.core.events import LoopFinished
from asgard.core.normalization import NormalizedPullRequest
from asgard.core.stage import RunContext, Stage
from asgard.prompts.evaluator import build_messages
from asgard.providers.errors import ProviderError
from asgard.providers.factory import LLMFactory
from asgard.providers.models import LLMResponse, Message, RoleName

_SUMMARY = ("pr_id", "status", "iterations", "classification", "evaluation")
_CAUGHT = (ProviderError, ValueError, ValidationError)


class _TraceLog:
    def __init__(self) -> None:
        self.pr_id = ""
        self.iteration = 0
        self.lines: list[dict[str, Any]] = []


class _RecordingFactory:
    """One trace line per public generate call, including a failed call."""

    def __init__(self, factory: LLMFactory, log: _TraceLog) -> None:
        self._factory = factory
        self._log = log

    def generate(
        self,
        role: RoleName,
        messages: list[Message],
        schema: type[BaseModel] | None = None,
    ) -> LLMResponse:
        spec = self._factory.settings.resolve(role)
        digest = _prompt_sha256(messages)
        try:
            response = self._factory.generate(role, messages, schema=schema)
        except Exception as exc:
            self._log.lines.append(
                _trace_line(
                    self._log,
                    role=role,
                    provider=spec.provider,
                    model=spec.model,
                    temperature=spec.temperature,
                    prompt_sha256=digest,
                    raw_response=None,
                    parsed=None,
                    input_tokens=None,
                    output_tokens=None,
                    latency_s=None,
                    error=str(exc),
                )
            )
            raise
        parsed = response.parsed
        if isinstance(parsed, BaseModel):
            parsed = _dump(parsed)
        self._log.lines.append(
            _trace_line(
                self._log,
                role=role,
                provider=spec.provider,
                model=spec.model,
                temperature=spec.temperature,
                prompt_sha256=digest,
                raw_response=response.text,
                parsed=parsed,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                latency_s=response.latency_s,
                error=None,
            )
        )
        return response


class ClassifyStage(Stage):
    name = "classify"

    def __init__(self, max_iterations: int, factory: LLMFactory) -> None:
        self.max_iterations = max_iterations
        self.factory = factory
        self._log = _TraceLog()
        self._traced = _RecordingFactory(factory, self._log)

    def execute(self, ctx: RunContext, data: Any) -> list[dict[str, Any]]:
        pulls = _pulls(data)

        def work(_item_id: str, pull: NormalizedPullRequest) -> dict[str, Any]:
            return self._review(ctx, pull)

        payloads = self.process_items(
            ctx,
            [(pull.context.pr_id, pull) for pull in pulls],
            work,
        )
        lines = [line for payload in payloads for line in payload["traces"]]
        directory = ctx.run_dir / self.name
        body = "".join(json.dumps(line) + "\n" for line in lines)
        _replace(directory / "trace.jsonl", body)
        _replace(directory / "metrics.json", json.dumps(_metrics(payloads)) + "\n")
        return [{key: payload[key] for key in _SUMMARY} for payload in payloads]

    def _review(self, ctx: RunContext, pull: NormalizedPullRequest) -> dict[str, Any]:
        self._log.pr_id = pull.context.pr_id
        self._log.lines = []
        classification: Classification | None = None
        evaluation: Evaluation | None = None
        status = "error"
        iterations = 0
        previous: str | None = None
        feedback: str | None = None

        for iteration in range(1, self.max_iterations + 1):
            self._log.iteration = iteration
            try:
                arrived = classify(
                    pull,
                    self._traced,
                    emit=ctx.emit,
                    previous=previous,
                    feedback=feedback,
                )
            except _CAUGHT:
                iterations = iteration
                status = "error"
                break
            iterations = iteration
            if classification is not None and arrived == classification:
                status = "unresolved_no_progress"
                break
            classification = arrived
            try:
                before = len(self._log.lines)
                reviewed = evaluate(pull, classification, self._traced, emit=ctx.emit)
            except _CAUGHT:
                status = "error"
                break
            evaluation = reviewed
            if not any(line["role"] == "evaluator" for line in self._log.lines[before:]):
                self._log.lines.append(self._deterministic(pull, classification, evaluation))
            if evaluation.verdict == "APPROVE":
                status = "approved"
                break
            if iteration == self.max_iterations:
                status = "unresolved_max_iter"
                break
            previous = classification.model_dump_json()
            feedback = evaluation.feedback

        ctx.emit(LoopFinished(pr_id=pull.context.pr_id))
        return {
            "pr_id": pull.context.pr_id,
            "status": status,
            "iterations": iterations,
            "classification": _dump(classification),
            "evaluation": _dump(evaluation),
            "traces": list(self._log.lines),
        }

    def _deterministic(
        self,
        pull: NormalizedPullRequest,
        classification: Classification,
        evaluation: Evaluation,
    ) -> dict[str, Any]:
        spec = self.factory.settings.resolve("evaluator")
        return _trace_line(
            self._log,
            role="evaluator",
            provider=spec.provider,
            model=spec.model,
            temperature=spec.temperature,
            prompt_sha256=_prompt_sha256(build_messages(pull, classification)),
            raw_response=None,
            parsed=evaluation.model_dump(mode="json"),
            input_tokens=None,
            output_tokens=None,
            latency_s=None,
            error=None,
        )


def _pulls(data: Any) -> list[NormalizedPullRequest]:
    if not isinstance(data, list):
        raise ConfigError("classify stage requires normalized pull requests")
    pulls: list[NormalizedPullRequest] = []
    for item in data:
        if isinstance(item, NormalizedPullRequest):
            pulls.append(item)
            continue
        if not isinstance(item, dict):
            raise ConfigError("classify stage requires normalized pull requests")
        try:
            pulls.append(NormalizedPullRequest.model_validate(item))
        except ValidationError:
            raise ConfigError("classify stage requires normalized pull requests") from None
    return pulls


def _dump(model: BaseModel | None) -> dict[str, Any] | None:
    if model is None:
        return None
    return model.model_dump(mode="json")


def _prompt_sha256(messages: list[Message]) -> str:
    payload = json.dumps([message.model_dump() for message in messages])
    return hashlib.sha256(payload.encode()).hexdigest()


def _trace_line(
    log: _TraceLog,
    *,
    role: str,
    provider: str,
    model: str,
    temperature: float,
    prompt_sha256: str,
    raw_response: str | None,
    parsed: Any,
    input_tokens: int | None,
    output_tokens: int | None,
    latency_s: float | None,
    error: str | None,
) -> dict[str, Any]:
    line: dict[str, Any] = {
        "pr_id": log.pr_id,
        "iteration": log.iteration,
        "role": role,
        "provider": provider,
        "model": model,
        "prompt_sha256": prompt_sha256,
        "temperature": temperature,
        "raw_response": raw_response,
        "parsed": parsed,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "latency_s": latency_s,
        "error": error,
    }
    if role == "evaluator":
        if isinstance(parsed, dict):
            line["verdict"] = parsed.get("verdict")
            line["findings"] = parsed.get("findings")
        else:
            line["verdict"] = None
            line["findings"] = None
    return line


def _metrics(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(payloads)
    if total == 0:
        return {
            "approval_rate_iteration_1": 0.0,
            "approval_rate_final": 0.0,
            "mean_iterations": 0.0,
            "decision_flips": 0,
        }
    first = sum(1 for payload in payloads if _approved_on_first(payload))
    final = sum(1 for payload in payloads if payload["status"] == "approved")
    iterations = sum(payload["iterations"] for payload in payloads)
    return {
        "approval_rate_iteration_1": first / total,
        "approval_rate_final": final / total,
        "mean_iterations": iterations / total,
        "decision_flips": _flips(payloads),
    }


def _approved_on_first(payload: dict[str, Any]) -> bool:
    for line in payload["traces"]:
        if line["role"] != "evaluator":
            continue
        return line.get("verdict") == "APPROVE" and not line.get("error")
    return False


def _flips(payloads: list[dict[str, Any]]) -> int:
    count = 0
    for payload in payloads:
        previous: bool | None = None
        for line in payload["traces"]:
            if line["role"] != "classifier" or line.get("error"):
                continue
            parsed = line.get("parsed")
            if not isinstance(parsed, dict):
                continue
            decision = parsed.get("has_security_discussion")
            if not isinstance(decision, bool):
                continue
            if previous is not None and decision != previous:
                count += 1
            previous = decision
    return count


def _replace(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
