"""Synthesize stage: map–reduce, batch size, and citation repair. No network."""

import json
import re
from pathlib import Path

import pytest

from asgard.core.config import ConfigError
from asgard.core.events import CitationWarning, SynthesisChunk
from asgard.core.stage import RunContext
from asgard.core.stages.synthesize import SynthesizeStage
from asgard.prompts.synthesizer import (
    build_map_messages,
    build_reduce_messages,
    recommendation_cap,
)
from asgard.providers.factory import LLMFactory
from asgard.providers.handlers.fake import FakeHandler
from asgard.providers.models import Credentials, LLMSettings, RoleSpec

_EMPTY = "There was no security discussion, so there is no guideline."


def _factory(handler: FakeHandler) -> LLMFactory:
    settings = LLMSettings(default=RoleSpec(provider="fake", model="fake"))
    return LLMFactory(settings, Credentials(), handlers={"fake": handler}, sleep=lambda _: None)


def _record(pr_id: str) -> dict[str, object]:
    return {
        "pr_id": pr_id,
        "title": "Fix authentication bypass",
        "link": f"https://github.com/acme/app/pull/{pr_id}",
        "evidence": [{"quote": "authentication bypass", "location": "description"}],
        "rationale": "token check",
    }


def _map_response(groups: list[tuple[str, list[str]]]) -> str:
    return json.dumps(
        {
            "items": [
                {
                    "owasp": owasp,
                    "title": f"Check {ids[0]}",
                    "risk": "Unsigned tokens are accepted.",
                    "actions": ["Reject unsigned tokens."],
                    "pr_ids": ids,
                }
                for owasp, ids in groups
            ]
        }
    )


def _reduce_response(groups: list[tuple[str, list[str]]], *, title: str = "Authentication") -> str:
    return json.dumps(
        {
            "recommendations": [
                {
                    "owasp": owasp,
                    "title": title if len(groups) == 1 else f"Check {ids[0]}",
                    "risk": "Unsigned tokens are accepted.",
                    "actions": ["Reject unsigned tokens."],
                    "sources": ids,
                }
                for owasp, ids in groups
            ]
        }
    )


def _payload(records: list[dict[str, object]]) -> str:
    rows = [
        {key: record[key] for key in ("pr_id", "evidence", "rationale")} for record in records
    ]
    return json.dumps(rows, indent=2)


def _run(
    tmp_path: Path,
    handler: FakeHandler,
    records: list[dict[str, object]],
    *,
    map_batch: int | None = None,
) -> tuple[str, list[object]]:
    events: list[object] = []
    stage = SynthesizeStage("acme/app", _factory(handler), map_batch=map_batch)
    output = stage.run(RunContext(tmp_path, events.append), records)
    return output, events


def _recommendation_ids(markdown: str) -> list[set[str]]:
    paragraphs = [block for block in markdown.split("\n\n") if block.startswith("**REC-")]
    return [set(re.findall(r"#(\d+)", paragraph)) for paragraph in paragraphs]


def test_empty_list_skips_the_model(tmp_path: Path) -> None:
    handler = FakeHandler(["should not be used"])
    output, events = _run(tmp_path, handler, [])
    assert output == _EMPTY
    assert handler.calls == []
    assert not any(isinstance(event, SynthesisChunk) for event in events)


def test_full_processing_sends_every_pull_request_in_one_map(tmp_path: Path) -> None:
    records = [_record(str(n)) for n in range(1, 22)]
    handler = FakeHandler(
        [
            _map_response([("A07", ["1"]), ("A01", ["21"])]),
            _reduce_response([("A07", ["1"]), ("A01", ["2"])]),
            json.dumps({"items": []}),
        ]
    )
    output, _events = _run(tmp_path, handler, records)
    assert len(handler.calls) == 3
    assert "These pull requests were not cited:" in handler.calls[2][1].content
    assert "#2" in handler.calls[2][1].content
    mapped = json.loads(handler.calls[0][1].content)
    assert len(mapped) == 21
    assert mapped[0]["pr_id"] == "1"
    assert mapped[-1]["pr_id"] == "21"
    reduced = json.loads(handler.calls[1][1].content)
    assert [item["id"] for item in reduced] == ["1", "2"]
    assert "at most 6 recommendations per category" in handler.calls[1][0].content
    assert _recommendation_ids(output) == [{"21"}, {"1"}]


def test_map_batches_use_the_chosen_size_and_reduce_stays_whole(tmp_path: Path) -> None:
    records = [_record(str(n)) for n in range(1, 22)]
    handler = FakeHandler(
        [
            _map_response([("A07", [str(n)]) for n in range(1, 11)]),
            _map_response([("A07", [str(n)]) for n in range(11, 21)]),
            _map_response([("A01", ["21"])]),
            _reduce_response([("A07", ["1"]), ("A01", ["21"])]),
            json.dumps({"items": []}),
        ]
    )
    output, _events = _run(tmp_path, handler, records, map_batch=10)
    assert len(handler.calls) == 5
    assert "These pull requests were not cited:" in handler.calls[4][1].content
    sizes = [len(json.loads(call[1].content)) for call in handler.calls[:3]]
    assert sizes == [10, 10, 1]
    reduced = json.loads(handler.calls[3][1].content)
    assert len(reduced) == 21
    assert reduced[0]["id"] == "1"
    assert reduced[-1]["id"] == "21"
    assert _recommendation_ids(output) == [{"21"}, {"1"}]


def test_map_then_reduce_for_one_call(tmp_path: Path) -> None:
    records = [_record("12"), _record("34")]
    handler = FakeHandler(
        [
            _map_response([("A07", ["12", "34"])]),
            _reduce_response([("A07", ["1"])]),
        ]
    )
    output, events = _run(tmp_path, handler, records)
    assert len(handler.calls) == 2
    assert handler.calls[0][0].content == build_map_messages("acme/app", records)[0].content
    assert handler.calls[0][1].content == _payload(records)
    assert "link" not in handler.calls[0][1].content
    assert '"title"' not in handler.calls[0][1].content
    mapped = json.loads(handler.calls[1][1].content)
    assert mapped[0]["id"] == "1"
    assert mapped[0]["actions"] == ["Reject unsigned tokens."]
    assert output.startswith("# app Security Policy — User Guidelines\n")
    assert "A07:2025 Authentication Failures" in output
    assert "- Reject unsigned tokens.\nRelated PRs: (#12, #34)" in output
    assert _recommendation_ids(output) == [{"12", "34"}]
    chunks = [event for event in events if isinstance(event, SynthesisChunk)]
    assert len(chunks) == 2


def test_empty_map_rules_skip_reduce(tmp_path: Path) -> None:
    records = [_record("12")]
    handler = FakeHandler(
        [json.dumps({"items": []}), _map_response([("A07", ["12"])])]
    )
    output, _events = _run(tmp_path, handler, records)
    assert len(handler.calls) == 2
    assert "These pull requests were not cited: #12." in handler.calls[1][1].content
    assert _recommendation_ids(output) == [{"12"}]


def test_reuses_existing_map_json(tmp_path: Path) -> None:
    records = [_record("12")]
    synthesize = tmp_path / "synthesize"
    synthesize.mkdir(parents=True, exist_ok=True)
    (synthesize / "map.json").write_text(
        json.dumps(
            [
                {
                    "owasp": "A07",
                    "title": "Validate tokens.",
                    "risk": "Bypass.",
                    "actions": ["Reject unsigned tokens."],
                    "pr_ids": ["12"],
                }
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    handler = FakeHandler([_reduce_response([("A07", ["1"])])])
    _run(tmp_path, handler, records)
    assert len(handler.calls) == 1
    assert json.loads(handler.calls[0][1].content)[0]["id"] == "1"


def test_outside_source_is_rejected_and_the_second_call_corrects(tmp_path: Path) -> None:
    records = [_record("12")]
    bad = _reduce_response([("A07", ["999"])])
    good = _reduce_response([("A07", ["1"])])
    handler = FakeHandler([_map_response([("A07", ["12"])]), bad, good])
    output, events = _run(tmp_path, handler, records, map_batch=10)
    assert len(handler.calls) == 3
    note = handler.calls[2][-1].content
    assert "Allowed ids: 1" in note
    assert "999" in note
    assert bad in note
    assert _recommendation_ids(output) == [{"12"}]
    chunks = [event.text for event in events if isinstance(event, SynthesisChunk)]
    assert chunks == [_map_response([("A07", ["12"])]), bad, good]


def test_outside_source_is_stripped_when_repair_keeps_it(tmp_path: Path) -> None:
    records = [_record("12")]
    mixed = _reduce_response([("A07", ["1", "999"])])
    handler = FakeHandler([_map_response([("A07", ["12"])]), mixed, mixed])
    output, events = _run(tmp_path, handler, records, map_batch=10)
    assert len(handler.calls) == 3
    assert _recommendation_ids(output) == [{"12"}]
    warnings = [event for event in events if isinstance(event, CitationWarning)]
    assert [event.pr_id for event in warnings] == ["999"]


def test_outside_only_source_still_fails_after_repair(tmp_path: Path) -> None:
    records = [_record("12")]
    bad = _reduce_response([("A07", ["999"])])
    handler = FakeHandler([_map_response([("A07", ["12"])]), bad, bad])
    with pytest.raises(ConfigError, match="outside the input"):
        _run(tmp_path, handler, records, map_batch=10)


def test_merged_sources_stay_one_recommendation(tmp_path: Path) -> None:
    records = [_record(str(n)) for n in range(1, 17)]
    handler = FakeHandler(
        [
            _map_response([("A07", [str(n) for n in range(1, 17)])]),
            _reduce_response([("A07", ["1", "2"])]),
        ]
    )
    output, _events = _run(tmp_path, handler, records, map_batch=20)
    assert len(handler.calls) == 2
    assert _recommendation_ids(output) == [{str(n) for n in range(1, 17)}]
    stored = json.loads((tmp_path / "synthesize" / "map.json").read_text(encoding="utf-8"))
    assert [len(item["pr_ids"]) for item in stored] == [15, 1]


def test_low_coverage_is_sent_back_once(tmp_path: Path) -> None:
    records = [_record(str(n)) for n in range(1, 6)]
    handler = FakeHandler(
        [
            _map_response([("A01", ["1"])]),
            _reduce_response([("A01", ["1"])]),
            _map_response([("A05", ["2", "3", "4", "5"])]),
        ]
    )
    output, _events = _run(tmp_path, handler, records)
    assert len(handler.calls) == 3
    note = handler.calls[2][1].content
    assert note.startswith(
        "These pull requests were not cited: #2, #3, #4, #5. "
        "Add or extend recommendations for those that fit."
    )
    assert _recommendation_ids(output) == [{"1"}, {"2", "3", "4", "5"}]


def test_same_title_is_merged_before_sources_become_pull_requests(tmp_path: Path) -> None:
    records = [_record("12"), _record("34")]
    handler = FakeHandler(
        [
            _map_response([("A07", ["12"]), ("A07", ["34"])]),
            json.dumps(
                {
                    "recommendations": [
                        {
                            "owasp": "A07",
                            "title": "Check tokens.",
                            "risk": "Unsigned tokens are accepted.",
                            "actions": ["Reject unsigned tokens."],
                            "sources": ["1"],
                        },
                        {
                            "owasp": "a07",
                            "title": "check tokens",
                            "risk": "A later copy.",
                            "actions": ["Ignore this copy."],
                            "sources": ["2"],
                        },
                    ]
                }
            ),
        ]
    )
    output, _events = _run(tmp_path, handler, records, map_batch=10)
    assert _recommendation_ids(output) == [{"12", "34"}]
    assert output.count("**REC-") == 1
    assert "Reject unsigned tokens." in output
    assert "Ignore this copy." not in output


def test_other_category_becomes_security_process(tmp_path: Path) -> None:
    records = [_record("12")]
    handler = FakeHandler(
        [
            _map_response([("OTHER", ["12"])]),
            _reduce_response([("other", ["1"])]),
        ]
    )
    output, _events = _run(tmp_path, handler, records)
    assert "## 1. Security Process" in output
    assert "A07:2025" not in output
    assert _recommendation_ids(output) == [{"12"}]


def test_recommendation_cap_is_thirty_percent_of_pull_requests() -> None:
    assert recommendation_cap(555) == 166
    assert recommendation_cap(98) == 29
    assert recommendation_cap(10) == 3
    assert recommendation_cap(1) == 1
    assert recommendation_cap(0) == 0


def test_build_map_messages_uses_override_and_keeps_json_braces() -> None:
    records = [_record("12")]
    messages = build_map_messages("acme/app", records, system_prompt="Map {repository}.")
    assert messages[0].content == "Map acme/app."
    assert messages[1].content == _payload(records)
    builtin = build_map_messages("acme/app", records)
    assert '{"items"' in builtin[0].content
    assert "- OTHER Security Process:" in builtin[0].content
    assert "{repository}" not in builtin[0].content
    assert "{owasp_categories}" not in builtin[0].content


def test_build_reduce_messages_uses_override_and_keeps_json_braces() -> None:
    items = [
        {
            "id": "1",
            "owasp": "A05",
            "title": "Do y.",
            "risk": "Z.",
            "actions": ["Check the input."],
        }
    ]
    messages = build_reduce_messages("acme/app", items, system_prompt="Reduce {repository}.")
    assert messages[0].content == "Reduce acme/app."
    assert messages[1].content == json.dumps(
        [
            {
                "id": "1",
                "owasp": "A05",
                "title": "Do y.",
                "risk": "Z.",
                "actions": ["Check the input."],
            }
        ],
        indent=2,
    )
    builtin = build_reduce_messages("acme/app", items)
    assert '{"recommendations"' in builtin[0].content
    assert "at most 1 recommendations per category" in builtin[0].content
    assert "- OTHER Security Process:" in builtin[0].content
    assert "{repository}" not in builtin[0].content
    assert "{max_recs}" not in builtin[0].content
    assert "{owasp_categories}" not in builtin[0].content


def test_stage_loads_map_and_reduce_prompt_overrides(tmp_path: Path) -> None:
    records = [_record("12")]
    synthesize = tmp_path / "synthesize"
    synthesize.mkdir(parents=True, exist_ok=True)
    (synthesize / "map_prompt.txt").write_text("Map {repository}.", encoding="utf-8")
    (synthesize / "reduce_prompt.txt").write_text("Reduce {repository}.", encoding="utf-8")
    handler = FakeHandler(
        [_map_response([("A07", ["12"])]), _reduce_response([("A07", ["1"])])]
    )
    _run(tmp_path, handler, records, map_batch=10)
    assert handler.calls[0][0].content == "Map acme/app."
    assert handler.calls[1][0].content == "Reduce acme/app."


def test_stage_loads_legacy_system_prompt_as_reduce(tmp_path: Path) -> None:
    records = [_record("12")]
    prompt = tmp_path / "synthesize" / "system_prompt.txt"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text("Legacy reduce {repository}.", encoding="utf-8")
    handler = FakeHandler(
        [_map_response([("A07", ["12"])]), _reduce_response([("A07", ["1"])])]
    )
    _run(tmp_path, handler, records, map_batch=10)
    assert handler.calls[0][0].content == build_map_messages("acme/app", records)[0].content
    assert handler.calls[1][0].content == "Legacy reduce acme/app."
