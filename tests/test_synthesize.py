"""Synthesize stage: one call, empty input, and citation repair. No network."""

import json
import re
from pathlib import Path

from asgard.core.events import SynthesisChunk
from asgard.core.stage import RunContext
from asgard.core.stages.synthesize import SynthesizeStage
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


def _response(groups: list[list[str]]) -> str:
    return json.dumps(
        {
            "topics": [
                {
                    "title": "Authentication",
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


def test_one_call_sends_the_full_list_and_cites_input_ids(tmp_path: Path) -> None:
    records = [_record("12"), _record("34")]
    handler = FakeHandler([_response([["12"], ["34", "12"]])])
    output, events = _run(tmp_path, handler, records)
    assert len(handler.calls) == 1
    assert handler.calls[0][1].role == "user"
    assert handler.calls[0][1].content == json.dumps(records, indent=2)
    assert output.startswith("# app Security Policy — User Guidelines\n")
    cited = _recommendation_ids(output)
    assert cited == [{"12"}, {"34", "12"}]
    chunks = [event for event in events if isinstance(event, SynthesisChunk)]
    assert len(chunks) == 1
    saved = json.loads((tmp_path / "synthesize" / "output.json").read_text(encoding="utf-8"))
    assert saved == output


def test_outside_citation_is_rejected_and_the_second_call_corrects(tmp_path: Path) -> None:
    records = [_record("12")]
    bad = _response([["999"]])
    good = _response([["12"]])
    handler = FakeHandler([bad, good])
    output, events = _run(tmp_path, handler, records)
    assert len(handler.calls) == 2
    note = handler.calls[1][-1].content
    assert "#12" in note
    assert "#999" in note
    assert bad in note
    assert _recommendation_ids(output) == [{"12"}]
    chunks = [event.text for event in events if isinstance(event, SynthesisChunk)]
    assert chunks == [bad, good]
