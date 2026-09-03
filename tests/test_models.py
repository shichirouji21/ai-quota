from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from ai_quota.models import (
    STATUS_OK,
    ProviderResult,
    Quota,
    UsageWindow,
    make_window,
)


def test_usage_window_is_frozen():
    w = UsageWindow(name="5h", used_percent=25.0, remaining_percent=75.0,
                    reset_at=None, duration_minutes=300)
    with pytest.raises(FrozenInstanceError):
        w.name = "week"  # frozen dataclass


def test_make_window_derives_remaining():
    w = make_window("5h", used_percent=27.0)
    assert w.remaining_percent == pytest.approx(73.0)
    assert w.duration_minutes is None
    assert w.reset_at is None


def test_make_window_clamps_out_of_range():
    assert make_window("x", used_percent=120.0).used_percent == 100.0
    assert make_window("x", used_percent=120.0).remaining_percent == 0.0
    assert make_window("x", used_percent=-5.0).used_percent == 0.0
    assert make_window("x", used_percent=-5.0).remaining_percent == 100.0


def test_provider_result_defaults():
    now = datetime.now(UTC)
    r = ProviderResult(provider="codex", status=STATUS_OK, fetched_at=now)
    assert r.windows == ()
    assert r.quotas == ()
    assert r.error is None
    assert r.raw is None


def test_quota_frozen():
    q = Quota(name="premium_interactions", unit="premium_requests",
              used=213, limit=25000, remaining=24787, remaining_percent=99.1,
              reset_at=None)
    with pytest.raises(FrozenInstanceError):
        q.used = 999
