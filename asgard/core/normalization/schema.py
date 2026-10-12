"""Canonical pull request schema. Bodies are stored in full, with no size limits."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

SCHEMA_VERSION = "1"


class PullRequestContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    pr_id: str
    title: str
    state: str
    is_merged: bool


class CodeReviewThread(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str
    comments: list[str]


class NormalizedPullRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = SCHEMA_VERSION
    context: PullRequestContext
    description: str
    general_discussion: list[str]
    code_review_threads: list[CodeReviewThread]
