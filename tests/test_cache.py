import json
from datetime import UTC, datetime, timedelta

import pytest

from ai_quota.cache import cache_dir, from_dict, load, store, to_dict
from ai_quota.models import STATUS_OK, ProviderResult, Quota, UsageWindow


@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    yield tmp_path / "ai-quota"


def _sample():
    now = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)
    return ProviderResult(
        provider="copilot",
        status=STATUS_OK,
        fetched_at=now,
        windows=(UsageWindow("5h", 27.0, 73.0, now + timedelta(hours=6), 300),),
        quotas=(Quota("premium_interactions", "premium_requests",
                     213, 25000, 24787, 99.1, now + timedelta(days=30)),),
        error=None,
        raw={"any": "value"},
    )


def test_cache_dir_created_with_0700(tmp_cache):
    d = cache_dir()
    assert d.exists()
    assert oct(d.stat().st_mode)[-3:] == "700"


def test_round_trip(tmp_cache):
    r = _sample()
    d = to_dict(r)
    r2 = from_dict(d)
    assert r2 == r


def test_store_and_load(tmp_cache):
    r = _sample()
    store(r)
    loaded = load("copilot", ttl_seconds=45, now=r.fetched_at + timedelta(seconds=10))
    assert loaded == r


def test_load_expired(tmp_cache):
    r = _sample()
    store(r)
    loaded = load("copilot", ttl_seconds=45, now=r.fetched_at + timedelta(seconds=60))
    assert loaded is None


def test_load_missing(tmp_cache):
    assert load("codex") is None


def test_file_permissions_0600(tmp_cache):
    r = _sample()
    store(r)
    f = cache_dir() / "copilot.json"
    assert oct(f.stat().st_mode)[-3:] == "600"


def test_store_redacts_raw_strings(tmp_cache):
    now = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)
    r = ProviderResult(provider="claude", status=STATUS_OK, fetched_at=now,
                       raw={"leak": "Authorization: Bearer abc123secretvalue"})
    store(r)
    body = json.loads((cache_dir() / "claude.json").read_text())
    assert "abc123secretvalue" not in body["raw"]["leak"]
