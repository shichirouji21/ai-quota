import time
from datetime import UTC, datetime

import pytest

from ai_quota import coordinator
from ai_quota.models import (
    STATUS_ERROR,
    STATUS_OK,
    ProviderResult,
)
from ai_quota.providers.base import Provider


class _StubOK(Provider):
    name = "stub_ok"

    def fetch(self, *, transport=None):
        return ProviderResult(provider=self.name, status=STATUS_OK,
                              fetched_at=datetime.now(UTC))


class _StubBoom(Provider):
    name = "stub_boom"

    def fetch(self, *, transport=None):
        raise RuntimeError("boom")


class _StubSlow(Provider):
    name = "stub_slow"

    def fetch(self, *, transport=None):
        time.sleep(2.0)
        return ProviderResult(provider=self.name, status=STATUS_OK,
                              fetched_at=datetime.now(UTC))


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))


def test_run_returns_all_requested(monkeypatch):
    monkeypatch.setitem(coordinator.PROVIDERS, "stub_ok", _StubOK)
    results = coordinator.run(providers=["stub_ok"], use_cache=False)
    assert set(results.keys()) == {"stub_ok"}
    assert results["stub_ok"].status == STATUS_OK


def test_run_isolates_exceptions(monkeypatch):
    monkeypatch.setitem(coordinator.PROVIDERS, "stub_boom", _StubBoom)
    monkeypatch.setitem(coordinator.PROVIDERS, "stub_ok", _StubOK)
    results = coordinator.run(providers=["stub_boom", "stub_ok"], use_cache=False)
    assert results["stub_boom"].status == STATUS_ERROR
    assert results["stub_ok"].status == STATUS_OK


def test_run_coordinator_timeout(monkeypatch):
    monkeypatch.setitem(coordinator.PROVIDERS, "stub_slow", _StubSlow)
    results = coordinator.run(providers=["stub_slow"], use_cache=False, global_timeout_s=0.3)
    assert results["stub_slow"].status == STATUS_ERROR
    assert "timeout" in (results["stub_slow"].error or "").lower()


def test_run_uses_cache(monkeypatch):
    calls = {"n": 0}

    class _CountingOK(Provider):
        name = "stub_count"

        def fetch(self, *, transport=None):
            calls["n"] += 1
            return ProviderResult(provider=self.name, status=STATUS_OK,
                                  fetched_at=datetime.now(UTC))

    monkeypatch.setitem(coordinator.PROVIDERS, "stub_count", _CountingOK)
    coordinator.run(providers=["stub_count"], use_cache=True)
    coordinator.run(providers=["stub_count"], use_cache=True)
    assert calls["n"] == 1


def test_run_bypasses_cache(monkeypatch):
    calls = {"n": 0}

    class _CountingOK(Provider):
        name = "stub_count2"

        def fetch(self, *, transport=None):
            calls["n"] += 1
            return ProviderResult(provider=self.name, status=STATUS_OK,
                                  fetched_at=datetime.now(UTC))

    monkeypatch.setitem(coordinator.PROVIDERS, "stub_count2", _CountingOK)
    coordinator.run(providers=["stub_count2"], use_cache=False)
    coordinator.run(providers=["stub_count2"], use_cache=False)
    assert calls["n"] == 2
