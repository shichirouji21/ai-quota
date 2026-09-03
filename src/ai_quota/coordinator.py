"""Coordinator: fan-out provider fetches with per-provider isolation and cache."""

from __future__ import annotations

import contextlib
from collections.abc import Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime

from ai_quota.cache import load as cache_load
from ai_quota.cache import store as cache_store
from ai_quota.models import STATUS_ERROR, STATUS_OK, ProviderResult
from ai_quota.providers.base import Provider
from ai_quota.providers.claude import ClaudeProvider
from ai_quota.providers.codex import CodexProvider
from ai_quota.providers.copilot import CopilotProvider

PROVIDERS: dict[str, type[Provider]] = {
    "codex": CodexProvider,
    "copilot": CopilotProvider,
    "claude": ClaudeProvider,
}


def _safe_fetch(cls: type[Provider]) -> ProviderResult:
    inst = cls()
    try:
        return inst.fetch()
    except Exception as e:  # noqa: BLE001
        return ProviderResult(
            provider=inst.name,
            status=STATUS_ERROR,
            fetched_at=datetime.now(UTC),
            error=str(e),
        )


def run(
    *,
    providers: Iterable[str],
    use_cache: bool = True,
    ttl_seconds: int = 45,
    global_timeout_s: float = 15.0,
) -> dict[str, ProviderResult]:
    requested = list(providers)
    results: dict[str, ProviderResult] = {}
    to_fetch: list[str] = []

    if use_cache:
        for name in requested:
            cached = cache_load(name, ttl_seconds=ttl_seconds)
            if cached is not None:
                results[name] = cached
            else:
                to_fetch.append(name)
    else:
        to_fetch = list(requested)

    if not to_fetch:
        return results

    futures: dict[Future, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(to_fetch))) as pool:
        for name in to_fetch:
            cls = PROVIDERS.get(name)
            if cls is None:
                results[name] = ProviderResult(
                    provider=name,
                    status=STATUS_ERROR,
                    fetched_at=datetime.now(UTC),
                    error=f"unknown provider: {name}",
                )
                continue
            futures[pool.submit(_safe_fetch, cls)] = name

        for fut, name in list(futures.items()):
            try:
                r = fut.result(timeout=global_timeout_s)
            except Exception:
                r = ProviderResult(
                    provider=name,
                    status=STATUS_ERROR,
                    fetched_at=datetime.now(UTC),
                    error="coordinator timeout",
                )
            results[name] = r
            if use_cache and r.status == STATUS_OK:
                with contextlib.suppress(OSError):
                    cache_store(r)

    return results
