"""Errors raised by provider calls. Messages never include API keys."""

import re

_STATUS_RE = re.compile(r"\b(408|429|503)\b")
_RETRY_RE = re.compile(r"retry in (\d+\.?\d*)", re.IGNORECASE)


class ProviderError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
        timed_out: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.timed_out = timed_out or status == 408

    @classmethod
    def from_exception(cls, exc: Exception) -> "ProviderError":
        if isinstance(exc, ProviderError):
            return exc
        status = _status(exc)
        name = type(exc).__name__.lower()
        timed_out = status == 408 or isinstance(exc, TimeoutError) or "timeout" in name
        return cls(str(exc), status=status, retry_after=_retry_after(exc), timed_out=timed_out)


class MissingAPIKey(ProviderError):
    def __init__(self, provider: str) -> None:
        super().__init__(f"missing API key for {provider}")
        self.provider = provider


def _status(exc: Exception) -> int | None:
    for attr in ("status_code", "status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    if isinstance(value, int):
        return value
    match = _STATUS_RE.search(str(exc))
    return int(match.group(1)) if match else None


def _retry_after(exc: Exception) -> float | None:
    match = _RETRY_RE.search(str(exc))
    if match:
        return float(match.group(1))
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    raw = headers.get("retry-after") if headers is not None and hasattr(headers, "get") else None
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
