"""Mine pull requests into the run directory."""

import json
import time
from collections.abc import Callable
from typing import Any

from asgard.core.config import ConfigError, RunConfig
from asgard.core.events import PRMined
from asgard.core.github_graphql import mine_pulls
from asgard.core.stage import RunContext, Stage


class MineStage(Stage):
    name = "mine"

    def __init__(
        self,
        *,
        opener: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.opener = opener
        self.sleep = sleep
        self.now = now

    def execute(self, ctx: RunContext, data: Any) -> list[int]:
        if not isinstance(data, RunConfig):
            raise ConfigError("mine stage requires a run configuration")
        token = data.github_token.get_secret_value()
        numbers, metrics = mine_pulls(
            ctx.run_dir,
            data.repo,
            token,
            filters=data.filters,
            page_size=data.mining.page_size,
            opener=self.opener,
            sleep=self.sleep,
            emit=ctx.emit,
            now=self.now,
        )
        path = ctx.run_dir / "mine" / "metrics.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(metrics), encoding="utf-8")
        temporary.replace(path)
        for number in numbers:
            ctx.emit(PRMined(pr_id=str(number)))
        return numbers
