import json
from datetime import UTC, datetime
from pathlib import Path

from ai_quota.models import (
    STATUS_AUTH_ERROR,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_UNAVAILABLE,
)
from ai_quota.providers.copilot import CopilotProvider, parse_copilot

FIXTURES = Path(__file__).parent / "fixtures" / "copilot"


def _body(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _now():
    return datetime(2026, 9, 2, 10, 0, tzinfo=UTC)


def test_parse_business_seat_ok():
    r = parse_copilot(_body("user_business.json"), fetched_at=_now())
    assert r.status == STATUS_OK
    assert r.provider == "copilot"
    names = {q.name for q in r.quotas}
    assert "premium_interactions" in names
    q = next(q for q in r.quotas if q.name == "premium_interactions")
    assert q.unit == "premium_requests"
    assert q.limit == 1000
    assert q.used == 123
    assert q.remaining == 877
    assert q.reset_at is not None
    assert q.reset_at.year == 2026 and q.reset_at.month == 10


def test_parse_business_hides_unlimited_from_quotas_but_keeps_raw():
    r = parse_copilot(_body("user_business.json"), fetched_at=_now())
    # unlimited quotas should not appear as headline quotas but MUST be in raw
    assert r.raw is not None
    assert "quota_snapshots" in r.raw
    assert r.raw["quota_snapshots"]["chat"]["unlimited"] is True
    # Neither chat nor completions (both unlimited) is in the headline list.
    assert not any(q.name == "chat" for q in r.quotas)
    assert not any(q.name == "completions" for q in r.quotas)


def test_parse_individual_ai_credits():
    r = parse_copilot(_body("user_individual_ai_credits.json"), fetched_at=_now())
    assert r.status == STATUS_OK
    names = {q.name for q in r.quotas}
    # Both metered quotas appear alongside premium_interactions.
    assert "premium_interactions" in names
    assert "chat" in names
    assert "completions" in names


def test_fetch_binary_missing():
    def transport():
        raise FileNotFoundError("gh")
    r = CopilotProvider().fetch(transport=transport)
    assert r.status == STATUS_UNAVAILABLE


def test_fetch_auth_error():
    def transport():
        return (4, "", "gh: You must run gh auth login")
    r = CopilotProvider().fetch(transport=transport)
    assert r.status == STATUS_AUTH_ERROR


def test_fetch_401():
    def transport():
        return (1, "", "gh: HTTP 401: Bad credentials")
    r = CopilotProvider().fetch(transport=transport)
    assert r.status == STATUS_AUTH_ERROR


def test_fetch_403_no_billing():
    def transport():
        return (1, "", "gh: HTTP 403: Resource not accessible")
    r = CopilotProvider().fetch(transport=transport)
    assert r.status == STATUS_AUTH_ERROR
    assert "permission" in (r.error or "").lower() or "403" in (r.error or "")


def test_fetch_malformed_json():
    def transport():
        return (0, "not json", "")
    r = CopilotProvider().fetch(transport=transport)
    assert r.status == STATUS_ERROR


def test_fetch_ok(tmp_path):
    body = (FIXTURES / "user_business.json").read_text()

    def transport():
        return (0, body, "")

    r = CopilotProvider().fetch(transport=transport)
    assert r.status == STATUS_OK
    assert r.quotas
