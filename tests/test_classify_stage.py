"""Classify stage loop, trace, and metrics. No network."""

import hashlib
import json
from pathlib import Path

import pytest

from asgard.core.classification import classify as classify_pull
from asgard.core.events import ClassifyProgress, LoopFinished
from asgard.core.normalization import NormalizedPullRequest, PullRequestContext
from asgard.core.orchestrator import Orchestrator
from asgard.core.stage import STAGE_NAMES, RunContext
from asgard.core.stages.classify import ClassifyStage
from asgard.providers.factory import LLMFactory
from asgard.providers.handlers.fake import FakeHandler
from asgard.providers.models import Credentials, LLMSettings, Message, RoleSpec

DESCRIPTION = "The login handler allows an authentication bypass."
_CRITERIA = ("decision", "evidence", "completeness", "rationale")


def _pull(pr_id: str = "42") -> NormalizedPullRequest:
    return NormalizedPullRequest(
        context=PullRequestContext(
            repository="acme/app",
            pr_id=pr_id,
            title="Fix authentication bypass",
            state="open",
            is_merged=False,
        ),
        description=DESCRIPTION,
        general_discussion=["Looks good to me."],
        code_review_threads=[],
    )


def _factory(handler: FakeHandler) -> LLMFactory:
    settings = LLMSettings(
        default=RoleSpec(provider="fake", model="fallback", temperature=0.9),
        classifier=RoleSpec(provider="fake", model="class-model", temperature=0.1),
        evaluator=RoleSpec(provider="fake", model="eval-model", temperature=0.2),
    )
    return LLMFactory(settings, Credentials(), handlers={"fake": handler}, sleep=lambda _: None)


def _classification(security: bool, rationale: str = "checked") -> str:
    if security:
        body: dict[str, object] = {
            "has_security_discussion": True,
            "evidence": [{"quote": "authentication bypass", "location": "description"}],
            "rationale": rationale,
        }
    else:
        body = {
            "has_security_discussion": False,
            "evidence": [],
            "rationale": rationale,
        }
    return json.dumps(body)


def _fabricated() -> str:
    return json.dumps(
        {
            "has_security_discussion": True,
            "evidence": [{"quote": "this quote was invented", "location": "description"}],
            "rationale": "invented",
        }
    )


def _findings(*, failed: str | None = None) -> list[dict[str, object]]:
    return [
        {
            "criterion": criterion,
            "passed": criterion != failed,
            "explanation": f"{criterion} checked",
        }
        for criterion in _CRITERIA
    ]


def _verdict(verdict: str, *, failed: str | None = None, feedback: str = "") -> str:
    return json.dumps(
        {"verdict": verdict, "findings": _findings(failed=failed), "feedback": feedback}
    )


def _run(
    tmp_path: Path,
    handler: FakeHandler,
    pulls: list[NormalizedPullRequest],
    *,
    max_iterations: int = 3,
    batch_size: int = 1,
    batch_max_chars: int | None = None,
) -> tuple[list[dict[str, object]], list[object]]:
    events: list[object] = []
    stage = ClassifyStage(
        max_iterations,
        _factory(handler),
        batch_size=batch_size,
        batch_max_chars=batch_max_chars,
    )
    ctx = RunContext(tmp_path, events.append)
    output = stage.run(ctx, [pull.model_dump() for pull in pulls])
    return output, events


def _classifier_calls(handler: FakeHandler) -> list[list[Message]]:
    return [call for call in handler.calls if call[0].content.startswith("You decide")]


def _batch_classifier_calls(handler: FakeHandler) -> list[list[Message]]:
    return [
        call
        for call in _classifier_calls(handler)
        if call[0].content.startswith("You decide whether each pull request")
    ]


def _solo_classifier_calls(handler: FakeHandler) -> list[list[Message]]:
    return [
        call
        for call in _classifier_calls(handler)
        if call[0].content.startswith("You decide whether one pull request")
    ]


def _evaluator_calls(handler: FakeHandler) -> list[list[Message]]:
    return [call for call in handler.calls if call[0].content.startswith("You review")]


def _batch_classification(entries: list[tuple[str, bool, str]]) -> str:
    results: list[dict[str, object]] = []
    for pr_id, security, rationale in entries:
        if security:
            item: dict[str, object] = {
                "pr_id": pr_id,
                "has_security_discussion": True,
                "evidence": [{"quote": "authentication bypass", "location": "description"}],
                "rationale": rationale,
            }
        else:
            item = {
                "pr_id": pr_id,
                "has_security_discussion": False,
                "evidence": [],
                "rationale": rationale,
            }
        results.append(item)
    return json.dumps({"results": results})


def _batch_verdict(
    pr_ids: list[str],
    verdict: str = "APPROVE",
    *,
    failed: str | None = None,
    feedback: str = "",
) -> str:
    return json.dumps(
        {
            "results": [
                {
                    "pr_id": pr_id,
                    "verdict": verdict,
                    "findings": _findings(failed=failed),
                    "feedback": feedback,
                }
                for pr_id in pr_ids
            ]
        }
    )


def _metrics(tmp_path: Path) -> dict[str, object]:
    return json.loads((tmp_path / "classify" / "metrics.json").read_text(encoding="utf-8"))


def _trace(tmp_path: Path) -> list[dict[str, object]]:
    text = (tmp_path / "classify" / "trace.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def _sha(messages: list[Message]) -> str:
    payload = json.dumps([message.model_dump() for message in messages])
    return hashlib.sha256(payload.encode()).hexdigest()


def test_revise_until_max_iterations(tmp_path: Path) -> None:
    handler = FakeHandler(
        [
            _classification(False, "one"),
            _verdict("REVISE", failed="decision", feedback="look again"),
            _classification(False, "two"),
            _verdict("REVISE", failed="completeness", feedback="still missing"),
        ]
    )

    output, _events = _run(tmp_path, handler, [_pull()], max_iterations=2)

    assert len(_classifier_calls(handler)) == 2
    assert output[0]["status"] == "unresolved_max_iter"
    assert output[0]["iterations"] == 2


def test_identical_classification_stops_without_another_evaluation(tmp_path: Path) -> None:
    same = _classification(False, "same")
    handler = FakeHandler([same, _verdict("REVISE", failed="decision", feedback="try again"), same])

    output, _events = _run(tmp_path, handler, [_pull()], max_iterations=3)

    assert output[0]["status"] == "unresolved_no_progress"
    assert len(_classifier_calls(handler)) == 2
    assert len(_evaluator_calls(handler)) == 1


def test_approve_on_first_iteration(tmp_path: Path) -> None:
    handler = FakeHandler([_classification(True), _verdict("APPROVE")])
    pull = _pull()

    output, events = _run(tmp_path, handler, [pull])

    assert output[0]["status"] == "approved"
    assert output[0]["iterations"] == 1
    assert [event for event in events if isinstance(event, LoopFinished)] == [
        LoopFinished(pr_id="42")
    ]
    assert DESCRIPTION in handler.calls[0][1].content
    assert pull.context.title in handler.calls[0][1].content


def test_fabricated_quote_skips_the_model_and_feeds_back(tmp_path: Path) -> None:
    handler = FakeHandler([_fabricated(), _fabricated()])

    output, _events = _run(tmp_path, handler, [_pull()], max_iterations=2)

    assert _evaluator_calls(handler) == []
    assert "this quote was invented" in handler.calls[1][1].content
    assert "literal" in handler.calls[1][1].content
    assert output[0]["status"] == "unresolved_no_progress"
    lines = [
        json.loads(line)
        for line in (tmp_path / "classify" / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    evaluator = next(line for line in lines if line["role"] == "evaluator")
    assert evaluator["verdict"] == "REVISE"
    assert evaluator["raw_response"] is None
    assert evaluator["input_tokens"] is None
    assert evaluator["output_tokens"] is None
    assert evaluator["latency_s"] is None


def test_invalid_json_keeps_prior_classification_and_continues(tmp_path: Path) -> None:
    first = _classification(False, "kept")
    handler = FakeHandler(
        [
            first,
            _verdict("REVISE", failed="decision", feedback="again"),
            "not json",
            "still not json",
            _classification(True, "next"),
            _verdict("APPROVE"),
        ]
    )

    output, _events = _run(tmp_path, handler, [_pull("1"), _pull("2")], max_iterations=2)

    assert output[0]["status"] == "error"
    assert output[0]["classification"]["rationale"] == "kept"
    assert output[0]["classification"]["has_security_discussion"] is False
    assert output[0]["evaluation"]["verdict"] == "REVISE"
    assert output[1]["status"] == "approved"
    assert output[1]["pr_id"] == "2"


def test_resume_skips_a_finished_pr_and_writes_output_at_the_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    killed = {"on": True}

    def wrapped(pull: NormalizedPullRequest, factory: LLMFactory, **kwargs: object) -> object:
        if pull.context.pr_id == "2" and killed["on"]:
            raise RuntimeError("killed")
        seen.append(pull.context.pr_id)
        return classify_pull(pull, factory, **kwargs)

    monkeypatch.setattr("asgard.core.stages.classify.classify", wrapped)
    handler = FakeHandler(
        [
            _classification(False, "first"),
            _verdict("APPROVE"),
            _classification(True, "second"),
            _verdict("APPROVE"),
        ]
    )
    pulls = [pull.model_dump() for pull in (_pull("1"), _pull("2"))]
    stage = ClassifyStage(1, _factory(handler))
    with pytest.raises(RuntimeError, match="killed"):
        Orchestrator([stage]).run(tmp_path, data=pulls)
    assert seen == ["1"]
    assert not (tmp_path / "classify" / "output.json").exists()

    killed["on"] = False
    seen.clear()
    result = Orchestrator([ClassifyStage(1, _factory(handler))]).run(tmp_path, data=pulls)
    assert seen == ["2"]
    assert [item["pr_id"] for item in result] == ["1", "2"]
    assert (tmp_path / "classify" / "output.json").is_file()
    trace = (tmp_path / "classify" / "trace.jsonl").read_text(encoding="utf-8")
    assert '"pr_id": "1"' in trace
    assert '"pr_id": "2"' in trace


def test_trace_reconstructs_the_call(tmp_path: Path) -> None:
    handler = FakeHandler([_classification(True, "because"), _verdict("APPROVE")])

    _run(tmp_path, handler, [_pull()])

    lines = [
        json.loads(line)
        for line in (tmp_path / "classify" / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [line["role"] for line in lines] == ["classifier", "evaluator"]
    classifier, evaluator = lines
    assert classifier["parsed"]["has_security_discussion"] is True
    assert classifier["parsed"]["rationale"] == "because"
    assert classifier["prompt_sha256"] == _sha(handler.calls[0])
    assert classifier["provider"] == "fake"
    assert classifier["model"] == "class-model"
    assert classifier["temperature"] == 0.1
    assert classifier["input_tokens"] == 3
    assert classifier["output_tokens"] == 5
    assert classifier["latency_s"] == 0.01
    assert classifier["error"] is None
    assert evaluator["verdict"] == "APPROVE"
    assert [finding["criterion"] for finding in evaluator["findings"]] == list(_CRITERIA)
    assert evaluator["prompt_sha256"] == _sha(handler.calls[1])
    assert evaluator["provider"] == "fake"
    assert evaluator["model"] == "eval-model"
    assert evaluator["temperature"] == 0.2
    assert evaluator["input_tokens"] == 3
    assert evaluator["output_tokens"] == 5
    assert evaluator["latency_s"] == 0.01
    saved = json.loads((tmp_path / "classify" / "output.json").read_text(encoding="utf-8"))
    assert "traces" not in saved[0]


def test_metrics_count_first_approval_and_a_decision_flip(tmp_path: Path) -> None:
    handler = FakeHandler(
        [
            _classification(True, "approved immediately"),
            _verdict("APPROVE"),
            _classification(False, "missed it"),
            _verdict("REVISE", failed="completeness", feedback="the description is security"),
            _classification(True, "now true"),
            _verdict("APPROVE"),
        ]
    )

    _run(tmp_path, handler, [_pull("1"), _pull("2")], max_iterations=2)

    assert _metrics(tmp_path) == {
        "approval_rate_iteration_1": 0.5,
        "approval_rate_final": 1.0,
        "mean_iterations": 1.5,
        "decision_flips": 1,
        "llm_calls": 6,
        "calls_per_pr": 3.0,
        "tokens_per_pr": {"input": 9.0, "output": 15.0},
        "single_pr_retries": 0,
    }


def test_pull_progress_is_the_place_in_the_set(tmp_path: Path) -> None:
    handler = FakeHandler(
        [
            _classification(False, "one"),
            _verdict("APPROVE"),
            _classification(False, "two"),
            _verdict("APPROVE"),
        ]
    )

    _output, events = _run(tmp_path, handler, [_pull("1"), _pull("2")])

    assert [event for event in events if isinstance(event, ClassifyProgress)] == [
        ClassifyProgress(kind="pull", index=1, total=2),
        ClassifyProgress(kind="pull", index=2, total=2),
    ]


def test_batch_progress_counts_each_group(tmp_path: Path) -> None:
    handler = FakeHandler(
        [
            _batch_classification([("1", False, "a"), ("2", False, "b")]),
            _batch_verdict(["1", "2"]),
            _classification(False, "c"),
            _verdict("APPROVE"),
        ]
    )

    _output, events = _run(
        tmp_path, handler, [_pull("1"), _pull("2"), _pull("3")], batch_size=2
    )

    assert [event for event in events if isinstance(event, ClassifyProgress)] == [
        ClassifyProgress(kind="batch", index=1, total=2),
        ClassifyProgress(kind="batch", index=2, total=2),
    ]


def test_batch_size_limits_classifier_calls_on_the_first_iteration(tmp_path: Path) -> None:
    handler = FakeHandler(
        [
            _batch_classification([("1", False, "a"), ("2", False, "b")]),
            _batch_verdict(["1", "2"]),
            _classification(False, "c"),
            _verdict("APPROVE"),
        ]
    )

    output, _events = _run(
        tmp_path, handler, [_pull("1"), _pull("2"), _pull("3")], batch_size=2
    )

    assert len(_classifier_calls(handler)) == 2
    assert [item["status"] for item in output] == ["approved", "approved", "approved"]
    lines = _trace(tmp_path)
    shared = [line for line in lines if "batch_id" in line]
    assert [line["pr_ids"] for line in shared] == [["1", "2"], ["1", "2"]]
    assert shared[0]["parsed"]["1"]["rationale"] == "a"
    assert shared[0]["parsed"]["2"]["has_security_discussion"] is False
    assert "pr_id" not in shared[0]
    assert shared[1]["parsed"]["1"]["verdict"] == "APPROVE"
    assert "verdict" not in shared[1]
    assert len({line["batch_id"] for line in shared}) == 2
    solo = [line for line in lines if line.get("pr_id") == "3"]
    assert [line["role"] for line in solo] == ["classifier", "evaluator"]
    assert "batch_id" not in solo[0]
    stored = {
        item["id"]: item["payload"]["traces"]
        for item in (
            json.loads(line)
            for line in (tmp_path / "classify" / "items.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        )
    }
    assert stored["1"][0]["batch_id"] == stored["2"][0]["batch_id"] == shared[0]["batch_id"]
    metrics = _metrics(tmp_path)
    assert metrics["llm_calls"] == 4
    assert metrics["calls_per_pr"] == pytest.approx(4 / 3)
    tokens = metrics["tokens_per_pr"]
    assert tokens["input"] == 4.0
    assert tokens["output"] == pytest.approx(20 / 3)
    assert metrics["single_pr_retries"] == 0
    assert metrics["approval_rate_iteration_1"] == 1.0
    assert metrics["decision_flips"] == 0


def test_batch_metrics_count_a_flip_and_split_shared_tokens(tmp_path: Path) -> None:
    handler = FakeHandler(
        [
            _batch_classification([("1", False, "no"), ("2", False, "no")]),
            _batch_verdict(["1", "2"], "REVISE", failed="decision", feedback="look again"),
            _batch_classification([("1", True, "yes"), ("2", True, "yes")]),
            _batch_verdict(["1", "2"]),
        ]
    )

    _run(tmp_path, handler, [_pull("1"), _pull("2")], max_iterations=2, batch_size=2)

    assert _metrics(tmp_path) == {
        "approval_rate_iteration_1": 0.0,
        "approval_rate_final": 1.0,
        "mean_iterations": 2.0,
        "decision_flips": 2,
        "llm_calls": 4,
        "calls_per_pr": 2.0,
        "tokens_per_pr": {"input": 6.0, "output": 10.0},
        "single_pr_retries": 0,
    }


def test_missing_batch_pr_is_retried_alone(tmp_path: Path) -> None:
    handler = FakeHandler(
        [
            _batch_classification([("1", False, "kept")]),
            _classification(True, "filled in"),
            _batch_verdict(["1", "2"]),
        ]
    )

    output, _events = _run(tmp_path, handler, [_pull("1"), _pull("2")], batch_size=2)

    solos = _solo_classifier_calls(handler)
    assert len(solos) == 1
    assert '"pr_id": "2"' in solos[0][1].content
    assert output[0]["status"] == "approved"
    assert output[1]["status"] == "approved"
    assert output[1]["classification"]["rationale"] == "filled in"
    assert _metrics(tmp_path)["single_pr_retries"] == 1


def test_invalid_batch_response_retries_each_pr_alone(tmp_path: Path) -> None:
    handler = FakeHandler(
        [
            "not json",
            "not json",
            _classification(False, "one"),
            _classification(False, "two"),
            _batch_verdict(["1", "2"]),
        ]
    )

    output, _events = _run(tmp_path, handler, [_pull("1"), _pull("2")], batch_size=2)

    assert len(_batch_classifier_calls(handler)) == 2
    assert len(_solo_classifier_calls(handler)) == 2
    assert [item["status"] for item in output] == ["approved", "approved"]
    metrics = _metrics(tmp_path)
    assert metrics["single_pr_retries"] == 2
    assert metrics["llm_calls"] == 4


def test_resume_rebatches_only_the_unsaved_pulls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    killed = {"on": True}
    original = ClassifyStage.save_item

    def wrapped(self: ClassifyStage, ctx: RunContext, item_id: str, payload: object) -> None:
        original(self, ctx, item_id, payload)
        if killed["on"]:
            killed["on"] = False
            raise RuntimeError("killed")

    monkeypatch.setattr(ClassifyStage, "save_item", wrapped)
    handler = FakeHandler(
        [
            _batch_classification([("1", False, "first"), ("2", False, "second")]),
            _batch_verdict(["1", "2"]),
            _batch_classification([("2", True, "again"), ("3", False, "third")]),
            _batch_verdict(["2", "3"]),
        ]
    )
    pulls = [pull.model_dump() for pull in (_pull("1"), _pull("2"), _pull("3"))]
    factory = _factory(handler)
    with pytest.raises(RuntimeError, match="killed"):
        Orchestrator([ClassifyStage(1, factory, batch_size=2)]).run(tmp_path, data=pulls)
    assert not (tmp_path / "classify" / "output.json").exists()
    saved = [
        json.loads(line)["id"]
        for line in (tmp_path / "classify" / "items.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert saved == ["1"]
    done = len(handler.calls)

    result = Orchestrator([ClassifyStage(1, factory, batch_size=2)]).run(tmp_path, data=pulls)

    resumed = _batch_classifier_calls(handler)[1:]
    assert len(handler.calls) - done == 2
    assert len(resumed) == 1
    assert '"pr_id": "1"' not in resumed[0][1].content
    assert '"pr_id": "2"' in resumed[0][1].content
    assert '"pr_id": "3"' in resumed[0][1].content
    assert [item["pr_id"] for item in result] == ["1", "2", "3"]
    assert result[0]["classification"]["rationale"] == "first"
    assert result[1]["classification"]["rationale"] == "again"


def test_batch_max_chars_splits_before_batch_size(tmp_path: Path) -> None:
    pulls = [_pull("1"), _pull("2"), _pull("3")]
    limit = min(len(pull.model_dump_json(indent=2)) for pull in pulls)
    handler = FakeHandler(
        [
            _classification(False, "one"),
            _verdict("APPROVE"),
            _classification(False, "two"),
            _verdict("APPROVE"),
            _classification(False, "three"),
            _verdict("APPROVE"),
        ]
    )

    output, _events = _run(tmp_path, handler, pulls, batch_size=4, batch_max_chars=limit)

    assert len(_classifier_calls(handler)) == 3
    assert _batch_classifier_calls(handler) == []
    assert [item["status"] for item in output] == ["approved", "approved", "approved"]


def test_evaluate_is_not_a_stage_name() -> None:
    assert "evaluate" not in STAGE_NAMES
    assert STAGE_NAMES == (
        "input",
        "mine",
        "normalize",
        "prefilter",
        "classify",
        "consolidate",
        "synthesize",
        "security_md",
        "bundle",
    )
