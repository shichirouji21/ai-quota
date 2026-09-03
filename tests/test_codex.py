import json
from datetime import UTC, datetime
from pathlib import Path

from ai_quota.models import (
    STATUS_AUTH_ERROR,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_UNAVAILABLE,
)
from ai_quota.providers.codex import CodexProvider, parse_codex_rate_limits

FIXTURES = Path(__file__).parent / "fixtures" / "codex"


def _now():
    return datetime(2026, 9, 2, 10, 0, tzinfo=UTC)


def _wrap_notification(rate_limits: dict) -> str:
    """Simulate the `codex debug app-server` line-prefixed output."""
    body = {
        "method": "account/rateLimits/updated",
        "params": {"rateLimits": rate_limits},
        "emittedAtMs": 1788354427000,
    }
    lines = json.dumps(body, indent=2).splitlines()
    return "> some client init\n" + "\n".join(f"< {ln}" for ln in lines) + "\n"


def test_parse_two_windows_ok():
    body = json.loads((FIXTURES / "rate_limits_ok.json").read_text())
    r = parse_codex_rate_limits(body, fetched_at=_now())
    assert r.status == STATUS_OK
    names = {w.name for w in r.windows}
    assert "5h" in names
    assert "weekly" in names
    for w in r.windows:
        assert 0.0 <= w.used_percent <= 100.0
        assert w.remaining_percent == 100.0 - w.used_percent
        assert w.reset_at is not None
        assert w.reset_at.tzinfo is not None


def test_parse_null_windows_still_ok():
    """When primary/secondary are null (e.g. no active windows) status is
    still ok; raw carries the full snapshot for the JSON consumer."""
    body = {"rateLimits": {
        "primary": None, "secondary": None,
        "planType": "plus", "rateLimitReachedType": None,
    }}
    r = parse_codex_rate_limits(body, fetched_at=_now())
    assert r.status == STATUS_OK
    assert r.windows == ()
    assert r.raw is not None
    assert r.raw["rateLimits"]["planType"] == "plus"


def test_parse_missing_ratelimits_error():
    r = parse_codex_rate_limits({"unexpected": True}, fetched_at=_now())
    assert r.status == STATUS_ERROR


def test_fetch_binary_missing():
    def transport():
        raise FileNotFoundError("codex")
    r = CodexProvider().fetch(transport=transport)
    assert r.status == STATUS_UNAVAILABLE


def test_fetch_auth_error_in_stderr():
    def transport():
        return "Error: not authenticated. Please run `codex login`."
    r = CodexProvider().fetch(transport=transport)
    assert r.status == STATUS_AUTH_ERROR


def test_fetch_no_notification_error():
    def transport():
        return "unrelated output with no rate limits notification\n"
    r = CodexProvider().fetch(transport=transport)
    assert r.status == STATUS_ERROR
    assert "notification" in (r.error or "").lower()


def test_fetch_ok_stub():
    body = json.loads((FIXTURES / "rate_limits_ok.json").read_text())
    payload = _wrap_notification(body["rateLimits"])

    def transport():
        return payload

    r = CodexProvider().fetch(transport=transport)
    assert r.status == STATUS_OK
    assert len(r.windows) == 2


def test_fetch_ok_null_windows():
    """Real state observed on this machine: no active windows."""
    payload = _wrap_notification({
        "primary": None, "secondary": None,
        "planType": None,
        "rateLimitReachedType": "workspace_member_credits_depleted",
        "credits": {"hasCredits": False, "unlimited": False, "balance": None},
    })

    def transport():
        return payload

    r = CodexProvider().fetch(transport=transport)
    assert r.status == STATUS_OK
    assert r.windows == ()
    assert r.raw["rateLimits"]["rateLimitReachedType"] == "workspace_member_credits_depleted"
