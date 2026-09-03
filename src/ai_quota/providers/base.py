"""Provider interface and shared helpers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, Protocol

from ai_quota.models import ProviderResult
from ai_quota.timeutils import now_local


class Transport(Protocol):
    def __call__(self, request: Any) -> Any: ...


class Provider(ABC):
    name: str

    @abstractmethod
    def fetch(self, *, transport: Callable[..., Any] | None = None) -> ProviderResult: ...


def error_result(provider: str, status: str, error: str) -> ProviderResult:
    return ProviderResult(
        provider=provider,
        status=status,
        fetched_at=now_local(),
        error=error,
    )
