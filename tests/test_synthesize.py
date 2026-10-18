"""Synthesize stage: map–reduce, empty input, and citation repair. No network."""

import json
import re
from pathlib import Path

import pytest

from asgard.core.config import ConfigError
from asgard.core.events import CitationWarning, SynthesisChunk
from asgard.core.stage import RunContext
from asgard.core.stages.synthesize import SynthesizeStage
from asgard.prompts.synthesizer import (
    MAP_PROMPT,
    REDUCE_PROMPT,
    build_map_messages,
    build_reduce_messages,
    map_records,
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


def _map_response(pr_ids: list[str]) -> str:
    return json.dumps(
        {
            "rules": [
                {
                    "pr_id": pr_id,
                    "artifact": "token check",
                    "rule": f"Validate tokens from pull {pr_id}.",
                    "risk": "Authentication can be bypassed.",
                }
                for pr_id in pr_ids
            ]
        }
    )


def _reduce_response(groups: list[list[str]], *, title: str = "Authentication") -> str:
    return json.dumps(
        {
            "topics": [
                {
                    "title": title,
                    "recommendations": [
                        {
                            "title": f"Check {index}",
                            "text": f"Reject unsigned tokens from pull {ids[0]}.",
                            "pr_ids": ids,
                        }
                        for index, ids in enumerate(groups, 1)
                    ],
                }
            ]
        }
    )


def _run(
    tmp_path: Path,
    handler: FakeHandler,
    records: list[dict[str, object]],
) -> tuple[str, list[object]]:
    events: list[object] = []
    stage = SynthesizeStage("acme/app", _factory(handler))
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
    saved = json.loads((tmp_path / "synthesize" / "output.json").read_text(encoding="utf-8"))
    assert saved == _EMPTY


def test_map_then_reduce_for_one_batch(tmp_path: Path) -> None:
    records = [_record("12"), _record("34")]
    handler = FakeHandler(
        [
            _map_response(["12", "34"]),
            _reduce_response([["12"], ["34", "12"]]),
        ]
    )
    output, events = _run(tmp_path, handler, records)
    assert len(handler.calls) == 2
    assert handler.calls[0][0].content == MAP_PROMPT.replace("{repository}", "acme/app")
    assert handler.calls[0][1].content == json.dumps(map_records(records), indent=2)
    assert "link" not in handler.calls[0][1].content
    assert handler.calls[1][0].content == REDUCE_PROMPT.replace("{repository}", "acme/app")
    mapped = json.loads(handler.calls[1][1].content)
    assert [rule["pr_id"] for rule in mapped] == ["12", "34"]
    assert output.startswith("# app Security Policy — User Guidelines\n")
    cited = _recommendation_ids(output)
    assert cited == [{"12"}, {"34", "12"}]
    chunks = [event for event in events if isinstance(event, SynthesisChunk)]
    assert len(chunks) == 2
    saved = json.loads((tmp_path / "synthesize" / "output.json").read_text(encoding="utf-8"))
    assert saved == output
    rules = json.loads((tmp_path / "synthesize" / "map.json").read_text(encoding="utf-8"))
    assert len(rules) == 2


def test_map_batches_of_twenty(tmp_path: Path) -> None:
    records = [_record(str(n)) for n in range(1, 22)]
    first = [str(n) for n in range(1, 21)]
    second = ["21"]
    handler = FakeHandler(
        [
            _map_response(first),
            _map_response(second),
            _reduce_response([["1"], ["21"]]),
        ]
    )
    _run(tmp_path, handler, records)
    assert len(handler.calls) == 3
    batch1 = json.loads(handler.calls[0][1].content)
    batch2 = json.loads(handler.calls[1][1].content)
    assert len(batch1) == 20
    assert len(batch2) == 1
    assert batch2[0]["pr_id"] == "21"


def test_empty_map_rules_skip_reduce(tmp_path: Path) -> None:
    records = [_record("12")]
    handler = FakeHandler([json.dumps({"rules": []})])
    output, _events = _run(tmp_path, handler, records)
    assert output == _EMPTY
    assert len(handler.calls) == 1


def test_reduce_batches_of_fifty_and_merges_topics(tmp_path: Path) -> None:
    records = [_record(str(n)) for n in range(1, 52)]
    map_ids = [str(n) for n in range(1, 21)]
    map_ids_2 = [str(n) for n in range(21, 41)]
    map_ids_3 = [str(n) for n in range(41, 52)]
    handler = FakeHandler(
        [
            _map_response(map_ids),
            _map_response(map_ids_2),
            _map_response(map_ids_3),
            _reduce_response([["1"]]),
            _reduce_response([["51"]], title="Auth"),
        ]
    )
    # Patch reduce batch size via module for this test would be ideal; 51 rules
    # with default batch 50 → 2 reduce calls. 51 map rules from 3 map batches.
    output, _events = _run(tmp_path, handler, records)
    assert len(handler.calls) == 5
    reduce_payloads = [json.loads(call[1].content) for call in handler.calls[3:]]
    assert len(reduce_payloads[0]) == 50
    assert len(reduce_payloads[1]) == 1
    assert "1. Authentication" in output
    assert "2. Auth" in output or "## 2." in output


def test_reuses_existing_map_json(tmp_path: Path) -> None:
    records = [_record("12")]
    synthesize = tmp_path / "synthesize"
    synthesize.mkdir(parents=True, exist_ok=True)
    (synthesize / "map.json").write_text(
        json.dumps(
            [
                {
                    "pr_id": "12",
                    "artifact": "token check",
                    "rule": "Validate tokens.",
                    "risk": "Bypass.",
                }
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    handler = FakeHandler([_reduce_response([["12"]])])
    _run(tmp_path, handler, records)
    assert len(handler.calls) == 1
    assert '"topics"' in handler.calls[0][1].content or "12" in handler.calls[0][1].content


def test_outside_citation_is_rejected_and_the_second_call_corrects(tmp_path: Path) -> None:
    records = [_record("12")]
    bad = _reduce_response([["999"]])
    good = _reduce_response([["12"]])
    handler = FakeHandler([_map_response(["12"]), bad, good])
    output, events = _run(tmp_path, handler, records)
    assert len(handler.calls) == 3
    note = handler.calls[2][-1].content
    assert "#12" in note
    assert "#999" in note
    assert bad in note
    assert _recommendation_ids(output) == [{"12"}]
    chunks = [event.text for event in events if isinstance(event, SynthesisChunk)]
    assert chunks == [_map_response(["12"]), bad, good]


def test_outside_citation_is_stripped_when_repair_keeps_it(tmp_path: Path) -> None:
    records = [_record("12")]
    mixed = _reduce_response([["12", "999"]])
    handler = FakeHandler([_map_response(["12"]), mixed, mixed])
    output, events = _run(tmp_path, handler, records)
    assert len(handler.calls) == 3
    assert _recommendation_ids(output) == [{"12"}]
    warnings = [event for event in events if isinstance(event, CitationWarning)]
    assert [event.pr_id for event in warnings] == ["999"]


def test_outside_only_citation_still_fails_after_repair(tmp_path: Path) -> None:
    records = [_record("12")]
    bad = _reduce_response([["999"]])
    handler = FakeHandler([_map_response(["12"]), bad, bad])
    with pytest.raises(ConfigError, match="outside the input"):
        _run(tmp_path, handler, records)


def test_build_map_messages_uses_override_and_keeps_json_braces() -> None:
    records = [_record("12")]
    messages = build_map_messages("acme/app", records, system_prompt="Map {repository}.")
    assert messages[0].content == "Map acme/app."
    assert messages[1].content == json.dumps(map_records(records), indent=2)
    builtin = build_map_messages("acme/app", records)
    assert '{"rules"' in builtin[0].content
    assert "{repository}" not in builtin[0].content


def test_build_reduce_messages_uses_override_and_keeps_json_braces() -> None:
    rules = [{"pr_id": "12", "artifact": "x", "rule": "Do y.", "risk": "Z."}]
    messages = build_reduce_messages("acme/app", rules, system_prompt="Reduce {repository}.")
    assert messages[0].content == "Reduce acme/app."
    assert messages[1].content == json.dumps(rules, indent=2)
    builtin = build_reduce_messages("acme/app", rules)
    assert '{"topics"' in builtin[0].content
    assert "{repository}" not in builtin[0].content


def test_stage_loads_map_and_reduce_prompt_overrides(tmp_path: Path) -> None:
    records = [_record("12")]
    synthesize = tmp_path / "synthesize"
    synthesize.mkdir(parents=True, exist_ok=True)
    (synthesize / "map_prompt.txt").write_text("Map {repository}.", encoding="utf-8")
    (synthesize / "reduce_prompt.txt").write_text("Reduce {repository}.", encoding="utf-8")
    handler = FakeHandler([_map_response(["12"]), _reduce_response([["12"]])])
    _run(tmp_path, handler, records)
    assert handler.calls[0][0].content == "Map acme/app."
    assert handler.calls[1][0].content == "Reduce acme/app."


def test_stage_loads_legacy_system_prompt_as_reduce(tmp_path: Path) -> None:
    records = [_record("12")]
    prompt = tmp_path / "synthesize" / "system_prompt.txt"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text("Legacy reduce {repository}.", encoding="utf-8")
    handler = FakeHandler([_map_response(["12"]), _reduce_response([["12"]])])
    _run(tmp_path, handler, records)
    assert handler.calls[0][0].content == MAP_PROMPT.replace("{repository}", "acme/app")
    assert handler.calls[1][0].content == "Legacy reduce acme/app."
