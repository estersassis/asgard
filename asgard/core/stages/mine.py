"""Mine pull requests into the run directory."""

import time
from collections.abc import Callable
from typing import Any

from asgard.core.config import ConfigError, RunConfig
from asgard.core.events import PRMined
from asgard.core.github import cache_pulls, list_pulls
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
        pulls = list_pulls(
            data.repo,
            token,
            state=data.filters.state,
            since=data.filters.since,
            until=data.filters.until,
            max_prs=data.filters.max_prs,
            opener=self.opener,
            sleep=self.sleep,
            emit=ctx.emit,
            now=self.now,
        )
        numbers = cache_pulls(
            ctx.run_dir,
            data.repo,
            token,
            pulls,
            opener=self.opener,
            sleep=self.sleep,
            emit=ctx.emit,
            now=self.now,
        )
        for number in numbers:
            ctx.emit(PRMined(pr_id=str(number)))
        return numbers
