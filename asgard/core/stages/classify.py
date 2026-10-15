"""Classify each pull request in a bounded review loop."""

import hashlib
import json
import logging
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from asgard.core.classification import (
    BatchClassification,
    BatchClassificationItem,
    Classification,
    classify,
)
from asgard.core.config import ConfigError
from asgard.core.evaluation import (
    BatchEvaluation,
    BatchEvaluationItem,
    Evaluation,
    deterministic_review,
    evaluate,
)
from asgard.core.events import ClassificationAttempt, EvaluatorVerdict, LoopFinished
from asgard.core.normalization import NormalizedPullRequest
from asgard.core.stage import RunContext, Stage
from asgard.prompts.classifier import build_batch_messages as classifier_batch_messages
from asgard.prompts.evaluator import build_batch_messages as evaluator_batch_messages
from asgard.prompts.evaluator import build_messages
from asgard.providers.errors import ProviderError
from asgard.providers.factory import LLMFactory
from asgard.providers.models import LLMResponse, Message, RoleName

logger = logging.getLogger("asgard.classify")

_SUMMARY = ("pr_id", "status", "iterations", "classification", "evaluation")
_CAUGHT = (ProviderError, ValueError, ValidationError)


class _TraceLog:
    def __init__(self) -> None:
        self.pr_id = ""
        self.iteration = 0
        self.lines: list[dict[str, Any]] = []
        self.batch_id: str | None = None
        self.pr_ids: list[str] | None = None


class _PullReview:
    """One pull request still inside the batched review loop."""

    def __init__(self, pull: NormalizedPullRequest) -> None:
        self.pull = pull
        self.iteration = 0
        self.classification: Classification | None = None
        self.evaluation: Evaluation | None = None
        self.previous: str | None = None
        self.feedback: str | None = None
        self.status = "error"
        self.traces: list[dict[str, Any]] = []
        self.done = False

    def payload(self) -> dict[str, Any]:
        return {
            "pr_id": self.pull.context.pr_id,
            "status": self.status,
            "iterations": self.iteration,
            "classification": _dump(self.classification),
            "evaluation": _dump(self.evaluation),
            "traces": self.traces,
        }


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

    def __init__(
        self,
        max_iterations: int,
        factory: LLMFactory,
        batch_size: int = 1,
        batch_max_chars: int | None = None,
    ) -> None:
        self.max_iterations = max_iterations
        self.factory = factory
        self.batch_size = batch_size
        self.batch_max_chars = batch_max_chars
        self._log = _TraceLog()
        self._traced = _RecordingFactory(factory, self._log)

    def execute(self, ctx: RunContext, data: Any) -> list[dict[str, Any]]:
        pulls = _pulls(data)
        if self.batch_size == 1:
            payloads = self._run_singly(ctx, pulls)
        else:
            payloads = self._run_batched(ctx, pulls)
        self._write_artifacts(ctx, payloads)
        return [{key: payload[key] for key in _SUMMARY} for payload in payloads]

    def _run_singly(
        self, ctx: RunContext, pulls: list[NormalizedPullRequest]
    ) -> list[dict[str, Any]]:
        def work(_item_id: str, pull: NormalizedPullRequest) -> dict[str, Any]:
            return self._review(ctx, pull)

        return self.process_items(
            ctx,
            [(pull.context.pr_id, pull) for pull in pulls],
            work,
        )

    def _run_batched(
        self, ctx: RunContext, pulls: list[NormalizedPullRequest]
    ) -> list[dict[str, Any]]:
        seen = self.completed_ids(ctx)
        reviews: list[_PullReview] = []
        for pull in pulls:
            pr_id = pull.context.pr_id
            if pr_id in seen:
                continue
            seen.add(pr_id)
            reviews.append(_PullReview(pull))

        while reviews:
            pending: list[_PullReview] = []
            for group in _groups(reviews, self.batch_size, self.batch_max_chars):
                self._review_group(ctx, group)
                for review in group:
                    if not review.done:
                        pending.append(review)
                        continue
                    self.save_item(ctx, review.pull.context.pr_id, review.payload())
                    ctx.emit(LoopFinished(pr_id=review.pull.context.pr_id))
            reviews = pending

        return _payloads_in_order(self._read_items(ctx), pulls)

    def _write_artifacts(self, ctx: RunContext, payloads: list[dict[str, Any]]) -> None:
        lines = _unique_trace_lines(payloads)
        directory = ctx.run_dir / self.name
        body = "".join(json.dumps(line) + "\n" for line in lines)
        _replace(directory / "trace.jsonl", body)
        _replace(directory / "metrics.json", json.dumps(_metrics(payloads)) + "\n")

    def _review_group(self, ctx: RunContext, group: list[_PullReview]) -> None:
        for review in group:
            review.iteration += 1
        self._classify_group(ctx, group)
        self._evaluate_group(ctx, [review for review in group if not review.done])
        for review in group:
            if not review.done:
                _finish_or_continue(review, self.max_iterations)

    def _classify_group(self, ctx: RunContext, group: list[_PullReview]) -> None:
        if len(group) == 1:
            self._classify_solo(ctx, group[0])
            return
        pr_ids = [review.pull.context.pr_id for review in group]
        response, lines = self._generate(
            "classifier",
            classifier_batch_messages(
                [(review.pull, review.previous, review.feedback) for review in group]
            ),
            BatchClassification,
            iteration=group[0].iteration,
            pr_ids=pr_ids,
        )
        _attach(group, lines)
        parsed = None if response is None else response.parsed
        if not isinstance(parsed, BatchClassification):
            for review in group:
                self._classify_solo(ctx, review)
            return
        chosen = _take_results(parsed.results, set(pr_ids), "classifier")
        for review in group:
            item = chosen.get(review.pull.context.pr_id)
            if not isinstance(item, BatchClassificationItem):
                self._classify_solo(ctx, review)
                continue
            self._apply_classification(ctx, review, _classification_from(item))

    def _classify_solo(self, ctx: RunContext, review: _PullReview) -> None:
        self._log.pr_id = review.pull.context.pr_id
        self._log.iteration = review.iteration
        self._log.batch_id = None
        self._log.pr_ids = None
        self._log.lines = []
        try:
            arrived = classify(
                review.pull,
                self._traced,
                emit=ctx.emit,
                previous=review.previous,
                feedback=review.feedback,
            )
        except _CAUGHT:
            review.traces.extend(self._log.lines)
            review.status = "error"
            review.done = True
            return
        review.traces.extend(self._log.lines)
        self._apply_classification(ctx, review, arrived, emit=False)

    def _apply_classification(
        self,
        ctx: RunContext,
        review: _PullReview,
        arrived: Classification,
        *,
        emit: bool = True,
    ) -> None:
        if emit:
            ctx.emit(ClassificationAttempt(pr_id=review.pull.context.pr_id))
        if review.classification is not None and arrived == review.classification:
            review.status = "unresolved_no_progress"
            review.done = True
            return
        review.classification = arrived

    def _evaluate_group(self, ctx: RunContext, group: list[_PullReview]) -> None:
        needs_model: list[_PullReview] = []
        for review in group:
            classification = review.classification
            if classification is None:
                review.status = "error"
                review.done = True
                continue
            revision = deterministic_review(review.pull, classification, emit=ctx.emit)
            if revision is None:
                needs_model.append(review)
                continue
            review.evaluation = revision
            self._log.pr_id = review.pull.context.pr_id
            self._log.iteration = review.iteration
            self._log.batch_id = None
            self._log.pr_ids = None
            review.traces.append(self._deterministic(review.pull, classification, revision))
        if not needs_model:
            return
        if len(needs_model) == 1:
            self._evaluate_solo(ctx, needs_model[0])
            return
        self._evaluate_batch(ctx, needs_model)

    def _evaluate_batch(self, ctx: RunContext, group: list[_PullReview]) -> None:
        pr_ids = [review.pull.context.pr_id for review in group]
        pairs: list[tuple[NormalizedPullRequest, Classification]] = []
        for review in group:
            classification = review.classification
            if classification is None:
                continue
            pairs.append((review.pull, classification))
        response, lines = self._generate(
            "evaluator",
            evaluator_batch_messages(pairs),
            BatchEvaluation,
            iteration=group[0].iteration,
            pr_ids=pr_ids,
        )
        _attach(group, lines)
        parsed = None if response is None else response.parsed
        if not isinstance(parsed, BatchEvaluation):
            for review in group:
                self._evaluate_solo(ctx, review)
            return
        chosen = _take_results(parsed.results, set(pr_ids), "evaluator")
        for review in group:
            item = chosen.get(review.pull.context.pr_id)
            if not isinstance(item, BatchEvaluationItem):
                self._evaluate_solo(ctx, review)
                continue
            evaluation = _evaluation_from(item)
            review.evaluation = evaluation
            ctx.emit(
                EvaluatorVerdict(
                    pr_id=review.pull.context.pr_id,
                    approved=evaluation.verdict == "APPROVE",
                )
            )

    def _evaluate_solo(self, ctx: RunContext, review: _PullReview) -> None:
        classification = review.classification
        if classification is None:
            review.status = "error"
            review.done = True
            return
        self._log.pr_id = review.pull.context.pr_id
        self._log.iteration = review.iteration
        self._log.batch_id = None
        self._log.pr_ids = None
        self._log.lines = []
        try:
            reviewed = evaluate(review.pull, classification, self._traced, emit=ctx.emit)
        except _CAUGHT:
            review.traces.extend(self._log.lines)
            review.status = "error"
            review.done = True
            return
        review.traces.extend(self._log.lines)
        if not any(line["role"] == "evaluator" for line in self._log.lines):
            review.traces.append(self._deterministic(review.pull, classification, reviewed))
        review.evaluation = reviewed

    def _generate(
        self,
        role: RoleName,
        messages: list[Message],
        schema: type[BaseModel],
        *,
        iteration: int,
        pr_ids: list[str],
    ) -> tuple[LLMResponse | None, list[dict[str, Any]]]:
        self._log.pr_id = ""
        self._log.iteration = iteration
        self._log.batch_id = uuid.uuid4().hex
        self._log.pr_ids = pr_ids
        self._log.lines = []
        response: LLMResponse | None
        try:
            try:
                response = self._traced.generate(role, messages, schema=schema)
            except _CAUGHT:
                response = None
            return response, list(self._log.lines)
        finally:
            self._log.batch_id = None
            self._log.pr_ids = None

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


def _groups(
    reviews: list[_PullReview],
    batch_size: int,
    batch_max_chars: int | None,
) -> list[list[_PullReview]]:
    groups: list[list[_PullReview]] = []
    current: list[_PullReview] = []
    current_chars = 0
    for review in reviews:
        size = len(review.pull.model_dump_json(indent=2))
        over_chars = batch_max_chars is not None and current_chars + size > batch_max_chars
        if current and (len(current) >= batch_size or over_chars):
            groups.append(current)
            current = []
            current_chars = 0
        current.append(review)
        current_chars += size
    if current:
        groups.append(current)
    return groups


def _attach(group: list[_PullReview], lines: list[dict[str, Any]]) -> None:
    for review in group:
        review.traces.extend(lines)


def _take_results(results: list[Any], expected: set[str], role: str) -> dict[str, Any]:
    chosen: dict[str, Any] = {}
    for item in results:
        pr_id = item.pr_id
        if pr_id not in expected:
            logger.warning("ignoring unknown pr_id %s in %s batch", pr_id, role)
            continue
        if pr_id in chosen:
            logger.warning("ignoring repeated pr_id %s in %s batch", pr_id, role)
            continue
        chosen[pr_id] = item
    return chosen


def _classification_from(item: BatchClassificationItem) -> Classification:
    return Classification(
        has_security_discussion=item.has_security_discussion,
        evidence=list(item.evidence),
        rationale=item.rationale,
    )


def _evaluation_from(item: BatchEvaluationItem) -> Evaluation:
    return Evaluation(
        verdict=item.verdict,
        findings=list(item.findings),
        feedback=item.feedback,
    )


def _finish_or_continue(review: _PullReview, max_iterations: int) -> None:
    if review.done:
        return
    classification = review.classification
    evaluation = review.evaluation
    if classification is None or evaluation is None:
        review.status = "error"
        review.done = True
        return
    if evaluation.verdict == "APPROVE":
        review.status = "approved"
        review.done = True
        return
    if review.iteration >= max_iterations:
        review.status = "unresolved_max_iter"
        review.done = True
        return
    review.previous = classification.model_dump_json()
    review.feedback = evaluation.feedback


def _payloads_in_order(
    items: list[dict[str, Any]],
    pulls: list[NormalizedPullRequest],
) -> list[dict[str, Any]]:
    stored = {item["id"]: item["payload"] for item in items}
    payloads: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pull in pulls:
        pr_id = pull.context.pr_id
        if pr_id in seen:
            continue
        seen.add(pr_id)
        payloads.append(stored[pr_id])
    return payloads


def _unique_trace_lines(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Write each shared call once, keeping every pull request's line order."""
    sequences = [payload["traces"] for payload in payloads]
    indexes = [0] * len(sequences)
    seen: set[str] = set()
    lines: list[dict[str, Any]] = []

    def skip_seen() -> None:
        for index, sequence in enumerate(sequences):
            while indexes[index] < len(sequence):
                batch_id = sequence[indexes[index]].get("batch_id")
                if isinstance(batch_id, str) and batch_id in seen:
                    indexes[index] += 1
                    continue
                break

    def blocked(batch_id: str) -> bool:
        for index, sequence in enumerate(sequences):
            for cursor in range(indexes[index], len(sequence)):
                if sequence[cursor].get("batch_id") != batch_id:
                    continue
                if cursor != indexes[index]:
                    return True
                break
        return False

    while True:
        skip_seen()
        pending = [
            index for index, sequence in enumerate(sequences) if indexes[index] < len(sequence)
        ]
        if not pending:
            break
        solo = [
            index
            for index in pending
            if not isinstance(sequences[index][indexes[index]].get("batch_id"), str)
        ]
        if solo:
            index = solo[0]
            lines.append(sequences[index][indexes[index]])
            indexes[index] += 1
            continue
        ready = [
            index for index in pending if not blocked(sequences[index][indexes[index]]["batch_id"])
        ]
        index = (ready or pending)[0]
        line = sequences[index][indexes[index]]
        batch_id = line.get("batch_id")
        if isinstance(batch_id, str):
            seen.add(batch_id)
        lines.append(line)
        indexes[index] += 1
    return lines


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
    if log.batch_id is not None:
        return {
            "batch_id": log.batch_id,
            "pr_ids": list(log.pr_ids or []),
            "iteration": log.iteration,
            "role": role,
            "provider": provider,
            "model": model,
            "prompt_sha256": prompt_sha256,
            "temperature": temperature,
            "raw_response": raw_response,
            "parsed": _parsed_by_pr(parsed),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "latency_s": latency_s,
            "error": error,
        }
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


def _parsed_by_pr(parsed: Any) -> Any:
    if not isinstance(parsed, dict):
        return parsed
    results = parsed.get("results")
    if not isinstance(results, list):
        return parsed
    indexed: dict[str, Any] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        pr_id = item.get("pr_id")
        if isinstance(pr_id, str) and pr_id not in indexed:
            indexed[pr_id] = item
    return indexed


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
