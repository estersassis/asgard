"""asgard pilot run / promote CLI. No network."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from asgard.cli.main import main
from asgard.core.prompt_registry import PromptRegistry
from asgard.providers.models import LLMResponse, Message

TOKEN = "ghp_secret"
_CRITERIA = ("decision", "evidence", "completeness", "rationale")


class _RecordingFake:
    def __init__(self) -> None:
        self.temperatures: list[float] = []

    def generate(
        self,
        messages: list[Message],
        *,
        schema: type | None = None,
        temperature: float = 0,
        timeout: float = 300,
    ) -> LLMResponse:
        self.temperatures.append(temperature)
        system = messages[0].content if messages else ""
        if system.startswith("You review"):
            return LLMResponse(
                text=_approve(),
                input_tokens=1,
                output_tokens=1,
                latency_s=0.01,
                model="fake",
            )
        security = "authentication bypass" in "\n".join(m.content for m in messages).lower()
        body = {
            "has_security_discussion": security,
            "evidence": (
                [{"quote": "authentication bypass", "location": "description"}]
                if security
                else []
            ),
            "rationale": "ok",
        }
        return LLMResponse(
            text=json.dumps(body),
            input_tokens=1,
            output_tokens=1,
            latency_s=0.01,
            model="fake",
        )


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


def _config(tmp_path: Path, *, classifier: str = "default") -> Path:
    path = tmp_path / "asgard.toml"
    path.write_text(
        "\n".join(
            [
                'repo = "owner/name"',
                f'output_dir = "{tmp_path / "out"}"',
                "",
                "[prompts]",
                f'classifier = "{classifier}"',
                f'registry_dir = "{tmp_path / "prompts"}"',
                "",
                "[llm.default]",
                'provider = "fake"',
                'model = "fake"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _registry(tmp_path: Path) -> tuple[str, str]:
    registry = PromptRegistry(tmp_path / "prompts")
    active = registry.save(
        "classifier",
        name="cli-active",
        body="You decide whether one pull request contains a security discussion.\nACTIVE",
    )
    candidate = registry.save(
        "classifier",
        name="cli-candidate",
        body="You decide whether one pull request contains a security discussion.\nCANDIDATE",
    )
    return active.id, candidate.id


def test_pilot_run_prints_metrics_and_writes_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setattr("asgard.core.config.load_dotenv", lambda path=Path(".env"): None)
    active_id, candidate_id = _registry(tmp_path)
    config = _config(tmp_path, classifier=active_id)
    out = tmp_path / "pilot-out"
    handler = _RecordingFake()

    main(
        [
            "pilot",
            "run",
            "--candidate",
            candidate_id,
            "--active",
            active_id,
            "--pilot-set",
            "example",
            "--config",
            str(config),
            "--output-dir",
            str(out),
            "--f1-min",
            "0.0",
            "--kappa-min",
            "0.0",
        ],
        handlers={"fake": handler},
    )

    printed = capsys.readouterr().out
    assert "candidate\tf1=" in printed
    assert "gates\t" in printed
    assert "confusion_candidate\ttp=" in printed
    assert "single_pass\tf1=" in printed
    assert (out / "metrics.json").is_file()
    assert (out / "predictions.jsonl").is_file()
    assert handler.temperatures
    assert all(temp == 0.0 for temp in handler.temperatures)


def test_pilot_promote_refuses_failed_gates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    config = _config(tmp_path, classifier="active")
    before = config.read_text(encoding="utf-8")
    metrics = tmp_path / "fail.json"
    metrics.write_text(
        json.dumps(
            {
                "candidate_prompt": "candidate-v1",
                "candidate": {
                    "accuracy": 0.5,
                    "precision": 0.5,
                    "recall": 0.5,
                    "f1": 0.5,
                    "kappa": 0.1,
                    "confusion": {"tp": 1, "fp": 1, "fn": 1, "tn": 1},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as caught:
        main(
            [
                "pilot",
                "promote",
                "--candidate",
                "candidate-v1",
                "--config",
                str(config),
                "--from-result",
                str(metrics),
            ]
        )
    assert caught.value.code == 2
    assert "cannot promote" in capsys.readouterr().err
    assert config.read_text(encoding="utf-8") == before


def test_pilot_promote_updates_config_from_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    config = _config(tmp_path, classifier="active")
    metrics = tmp_path / "ok.json"
    metrics.write_text(
        json.dumps(
            {
                "candidate_prompt": "candidate-v1",
                "gates": {
                    "passed": True,
                    "can_promote": True,
                    "failures": [],
                    "thresholds": {"f1_min": 0.9, "kappa_min": 0.8},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    main(
        [
            "pilot",
            "promote",
            "--candidate",
            "candidate-v1",
            "--config",
            str(config),
            "--from-result",
            str(metrics),
        ]
    )
    assert 'classifier = "candidate-v1"' in config.read_text(encoding="utf-8")
    assert "promoted classifier=candidate-v1" in capsys.readouterr().out
