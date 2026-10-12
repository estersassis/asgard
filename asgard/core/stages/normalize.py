"""Turn mined pull requests into the canonical document."""

import json
from typing import Any

from asgard.core.config import ConfigError
from asgard.core.normalization import NormalizedPullRequest, format_pull
from asgard.core.stage import RunContext, Stage


class NormalizeStage(Stage):
    name = "normalize"

    def __init__(self, repo: str | None = None) -> None:
        self.repo = repo

    def execute(self, ctx: RunContext, data: Any) -> list[dict[str, Any]]:
        numbers = _numbers(data)

        def work(_item_id: str, number: int) -> dict[str, Any]:
            path = ctx.run_dir / "mine" / "prs" / f"{number}.json"
            if not path.is_file():
                raise ConfigError(f"mined pull {number} is missing")
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ConfigError(f"mined pull {number} was not an object")
            formatted = format_pull(payload, repo=self.repo)
            return NormalizedPullRequest.model_validate(formatted).model_dump()

        return self.process_items(ctx, [(str(number), number) for number in numbers], work)


def _numbers(data: Any) -> list[int]:
    if isinstance(data, list) and all(isinstance(item, int) for item in data):
        return data
    raise ConfigError("normalize stage requires pull request numbers")
