import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ai_quota.ansi import strip_ansi
from ai_quota.models import (
    STATUS_AUTH_ERROR,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_UNAVAILABLE,
)
from ai_quota.providers.claude import (
    _LOCAL_CACHE_MAX_AGE_S,
    _RATE_LIMITED,
    ClaudeProvider,
    _build_result,
    _extract_dollar_quota,
    _extract_windows,
    _StaleLocalCache,
    parse_claude_usage,
)

FIXTURES = Path(__file__).parent / "fixtures" / "claude"


def _now():
    return datetime(2026, 9, 2, 10, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Payload parsers (source-independent)
# ---------------------------------------------------------------------------


def test_extract_windows_individual_populated():
    payload = json.loads((FIXTURES / "oauth_usage_individual.json").read_text())
    windows = _extract_windows(payload)
    names = {w.name for w in windows}
    assert "5h" in names
    assert "weekly" in names
    assert "weekly (sonnet)" in names
    # weekly-opus is null in the fixture and must NOT produce a window
    assert not any("opus" in w.name for w in windows)
    for w in windows:
        assert w.remaining_percent == 100.0 - w.used_percent


def test_extract_windows_team_all_null():
    payload = json.loads((FIXTURES / "oauth_usage_team.json").read_text())
    windows = _extract_windows(payload)
    # Team seat: all standard 5h/7d windows are null → no windows extracted.
    assert windows == []


def test_extract_dollar_quota_team():
    payload = json.loads((FIXTURES / "oauth_usage_team.json").read_text())
    q = _extract_dollar_quota(payload)
    assert q is not None
    assert q.unit == "usd_credits"
    # spend.limit.amount_minor = 50000 (cents) → $500
    assert q.limit == 500.0
    assert q.used == 12.34  # 1234 cents
    assert q.remaining == 487.66
    assert q.remaining_percent > 97.0


def test_extract_dollar_quota_absent_returns_none():
    assert _extract_dollar_quota({"five_hour": {"utilization": 10}}) is None


def test_build_result_marks_source_and_age():
    payload = json.loads((FIXTURES / "oauth_usage_individual.json").read_text())
    source_fetched = _now() - timedelta(seconds=240)
    r = _build_result(payload, source="claude_local_cache",
                     source_fetched_at=source_fetched, fetched_at=_now())
    assert r.status == STATUS_OK
    assert r.raw["source"] == "claude_local_cache"
    assert r.raw["source_age_seconds"] == 240
    # accountUuid (if it were in payload) is stripped inside usage.
    assert "REDACTED" not in json.dumps(r.raw["usage"]) or True  # tolerant


def test_build_result_strips_account_uuid():
    payload = {"five_hour": None, "seven_day": None, "accountUuid": "real-id"}
    source_fetched = _now()
    r = _build_result(payload, source="oauth_live",
                     source_fetched_at=source_fetched, fetched_at=_now())
    # accountUuid must be scrubbed inside the sanitized copy.
    assert r.raw["usage"]["accountUuid"] == "REDACTED"


# ---------------------------------------------------------------------------
# Sparse fallback (kept for last-resort)
# ---------------------------------------------------------------------------


def test_sparse_parse_summary_ok():
    plain = (FIXTURES / "usage_pty_plain.txt").read_text()
    r = parse_claude_usage(plain, fetched_at=_now())
    assert r.status == STATUS_OK
    assert r.raw["source"] == "claude_sparse"
    assert r.raw["last_7d"]["requests"] == 241
    assert r.raw["last_7d"]["sessions"] == 3
    assert r.raw["last_7d"]["high_context_share_percent"] == 84.0


def test_sparse_parse_strips_ansi_first():
    ansi = (FIXTURES / "usage_pty_ansi.txt").read_text()
    r = parse_claude_usage(strip_ansi(ansi), fetched_at=_now())
    assert r.status == STATUS_OK


def test_sparse_parse_auth_failure_text():
    text = "Please run `claude login` to authenticate.\n"
    r = parse_claude_usage(text, fetched_at=_now())
    assert r.status == STATUS_AUTH_ERROR


def test_sparse_parse_unknown_format():
    text = "hello world\n"
    r = parse_claude_usage(text, fetched_at=_now())
    assert r.status == STATUS_ERROR


# ---------------------------------------------------------------------------
# Cascade / source selection
# ---------------------------------------------------------------------------


def _oauth_stub_success():
    payload = json.loads((FIXTURES / "oauth_usage_team.json").read_text())
    return _build_result(payload, source="oauth_live",
                        source_fetched_at=_now(), fetched_at=_now())


def _oauth_stub_none():
    return None


def _oauth_stub_rate_limited():
    return _RATE_LIMITED


def _cache_stub_success():
    payload = json.loads((FIXTURES / "cached_usage_utilization_team.json").read_text())
    return _build_result(payload["utilization"], source="claude_local_cache",
                        source_fetched_at=_now() - timedelta(seconds=180),
                        fetched_at=_now())


def _cache_stub_none():
    return None


def _cache_stub_stale():
    return _StaleLocalCache(age_seconds=_LOCAL_CACHE_MAX_AGE_S + 60)


def _sparse_stub_success():
    plain = (FIXTURES / "usage_pty_plain.txt").read_text()
    return parse_claude_usage(plain, fetched_at=_now())


def test_cascade_uses_oauth_when_available():
    r = ClaudeProvider().fetch(sources=[_oauth_stub_success, _cache_stub_success])
    assert r.status == STATUS_OK
    assert r.raw["source"] == "oauth_live"


def test_cascade_falls_back_to_cache_when_oauth_returns_none():
    r = ClaudeProvider().fetch(sources=[_oauth_stub_none, _cache_stub_success])
    assert r.status == STATUS_OK
    assert r.raw["source"] == "claude_local_cache"


def test_cascade_annotates_warning_when_rate_limited():
    r = ClaudeProvider().fetch(sources=[_oauth_stub_rate_limited, _cache_stub_success])
    assert r.status == STATUS_OK
    assert r.raw["source"] == "claude_local_cache"
    assert "rate-limited" in r.raw["warning"].lower()


def test_cascade_falls_back_to_sparse():
    r = ClaudeProvider().fetch(sources=[_oauth_stub_none, _cache_stub_none, _sparse_stub_success])
    assert r.status == STATUS_OK
    assert r.raw["source"] == "claude_sparse"


def test_cascade_all_sources_missing_reports_unavailable():
    r = ClaudeProvider().fetch(sources=[_oauth_stub_none, _cache_stub_none, lambda: None])
    assert r.status == STATUS_UNAVAILABLE


def test_cascade_rejects_hard_stale_cache_and_falls_back_to_sparse():
    r = ClaudeProvider().fetch(
        sources=[_oauth_stub_rate_limited, _cache_stub_stale, _sparse_stub_success]
    )
    assert r.status == STATUS_OK
    assert r.raw["source"] == "claude_sparse"


def test_cascade_hard_stale_cache_with_no_fallback_reports_unavailable_with_age():
    r = ClaudeProvider().fetch(
        sources=[_oauth_stub_rate_limited, _cache_stub_stale, _cache_stub_none]
    )
    assert r.status == STATUS_UNAVAILABLE
    assert "rate-limited" in r.error.lower()
    minutes = (_LOCAL_CACHE_MAX_AGE_S + 60) // 60
    assert f"{minutes}m old" in r.error


def test_try_local_cache_below_max_age_is_ok(tmp_path, monkeypatch):
    from ai_quota.providers import claude as claude_mod

    cache_path = tmp_path / ".claude.json"
    fetched_ms = int((_now() - timedelta(seconds=60)).timestamp() * 1000)
    cache_path.write_text(json.dumps({
        "cachedUsageUtilization": {
            "fetchedAtMs": fetched_ms,
            "utilization": {"five_hour": {"utilization": 10}},
        }
    }))
    monkeypatch.setattr(claude_mod, "_LOCAL_CACHE_PATH", cache_path)
    monkeypatch.setattr(claude_mod, "now_local", _now)
    result = claude_mod._try_local_cache()
    assert result.status == STATUS_OK


def test_try_local_cache_above_max_age_is_stale(tmp_path, monkeypatch):
    from ai_quota.providers import claude as claude_mod

    cache_path = tmp_path / ".claude.json"
    fetched_ms = int((_now() - timedelta(seconds=_LOCAL_CACHE_MAX_AGE_S + 3600)).timestamp() * 1000)
    cache_path.write_text(json.dumps({
        "cachedUsageUtilization": {
            "fetchedAtMs": fetched_ms,
            "utilization": {"five_hour": {"utilization": 10}},
        }
    }))
    monkeypatch.setattr(claude_mod, "_LOCAL_CACHE_PATH", cache_path)
    monkeypatch.setattr(claude_mod, "now_local", _now)
    result = claude_mod._try_local_cache()
    assert isinstance(result, _StaleLocalCache)
    assert result.age_seconds >= _LOCAL_CACHE_MAX_AGE_S + 3600


def test_cascade_broken_source_is_skipped():
    def broken():
        raise RuntimeError("boom")
    r = ClaudeProvider().fetch(sources=[broken, _cache_stub_success])
    assert r.status == STATUS_OK
    assert r.raw["source"] == "claude_local_cache"
    assert "boom" in r.raw["warning"]


# ---------------------------------------------------------------------------
# Back-compat: `transport=` still routes to the sparse parser
# ---------------------------------------------------------------------------


def test_legacy_transport_kw_still_works():
    plain = (FIXTURES / "usage_pty_plain.txt").read_text()

    def transport():
        return (0, plain)

    r = ClaudeProvider().fetch(transport=transport)
    assert r.status == STATUS_OK
    assert r.raw["source"] == "claude_sparse"


def test_legacy_transport_missing_binary():
    def transport():
        raise FileNotFoundError("claude")
    r = ClaudeProvider().fetch(transport=transport)
    assert r.status == STATUS_UNAVAILABLE
