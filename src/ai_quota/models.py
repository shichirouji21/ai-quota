"""Normalized domain model for ai-quota. Pure, no side effects."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

STATUS_OK = "ok"
STATUS_AUTH_ERROR = "auth_error"
STATUS_UNAVAILABLE = "unavailable"
STATUS_ERROR = "error"


@dataclass(frozen=True)
class UsageWindow:
    name: str
    used_percent: float
    remaining_percent: float
    reset_at: datetime | None
    duration_minutes: int | None


@dataclass(frozen=True)
class Quota:
    name: str
    unit: str
    used: float
    limit: float
    remaining: float
    remaining_percent: float
    reset_at: datetime | None


@dataclass(frozen=True)
class ProviderResult:
    provider: str
    status: str
    fetched_at: datetime
    windows: tuple[UsageWindow, ...] = ()
    quotas: tuple[Quota, ...] = ()
    error: str | None = None
    raw: Mapping[str, Any] | None = None


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    if value < low:
        return low
    if value > high:
        return high
    return value


def make_window(
    name: str,
    used_percent: float,
    reset_at: datetime | None = None,
    duration_minutes: int | None = None,
) -> UsageWindow:
    used = _clamp(float(used_percent))
    return UsageWindow(
        name=name,
        used_percent=used,
        remaining_percent=_clamp(100.0 - used),
        reset_at=reset_at,
        duration_minutes=duration_minutes,
    )
