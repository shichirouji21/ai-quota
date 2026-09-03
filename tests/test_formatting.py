import json
from datetime import UTC, datetime, timedelta

from ai_quota.formatting import progress_bar, render_human, render_json, render_statusline
from ai_quota.models import (
    STATUS_AUTH_ERROR,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    ProviderResult,
    Quota,
    UsageWindow,
)


def _now():
    return datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def _sample_copilot():
    return ProviderResult(
        provider="copilot", status=STATUS_OK, fetched_at=_now(),
        quotas=(Quota("premium_interactions", "premium_requests",
                     213, 25000, 24787, 99.1, _now() + timedelta(days=30)),),
        raw={"whatever": True},
    )


def _sample_codex_windows():
    return ProviderResult(
        provider="codex", status=STATUS_OK, fetched_at=_now(),
        windows=(
            UsageWindow("5h", 27.0, 73.0, _now() + timedelta(hours=4, minutes=20), 300),
            UsageWindow("weekly", 59.0, 41.0, _now() + timedelta(days=3), 10080),
        ),
    )


def test_json_schema_shape():
    results = {"copilot": _sample_copilot(), "codex": _sample_codex_windows()}
    s = render_json(results, generated_at=_now())
    doc = json.loads(s)
    assert "generated_at" in doc
    assert set(doc["providers"].keys()) == {"copilot", "codex"}
    codex = doc["providers"]["codex"]
    assert codex["status"] == "ok"
    assert len(codex["windows"]) == 2
    assert codex["windows"][0]["name"] == "5h"
    assert codex["windows"][0]["used_percent"] == 27.0
    assert "T" in codex["windows"][0]["reset_at"]
    copilot = doc["providers"]["copilot"]
    assert copilot["quota"]["unit"] == "premium_requests"


def test_json_includes_failed_provider():
    err = ProviderResult(provider="copilot", status=STATUS_AUTH_ERROR, fetched_at=_now(),
                        error="GitHub authentication is missing")
    doc = json.loads(render_json({"copilot": err}, generated_at=_now()))
    p = doc["providers"]["copilot"]
    assert p["status"] == "auth_error"
    assert "authentication" in p["error"].lower()


def test_human_lists_provider_headers():
    out = render_human({"copilot": _sample_copilot()}, use_color=False, use_unicode=False,
                       now=_now())
    assert "Copilot" in out
    assert "25000" in out or "24787" in out or "premium" in out.lower()


def test_human_shows_unavailable_reason_not_traceback():
    err = ProviderResult(provider="copilot", status=STATUS_AUTH_ERROR, fetched_at=_now(),
                        error="GitHub authentication is missing")
    out = render_human({"copilot": err}, use_color=False, use_unicode=False, now=_now())
    assert "Copilot" in out
    assert "unavailable" in out.lower() or "auth" in out.lower()
    assert "Traceback" not in out


def test_progress_bar_ascii_when_no_unicode():
    bar = progress_bar(30.0, width=10, use_unicode=False)
    assert set(bar).issubset(set("[]#-"))
    assert len(bar) == 12  # [########--] shape


def test_progress_bar_unicode_ok():
    bar = progress_bar(30.0, width=10, use_unicode=True)
    assert len(bar) >= 10


def test_compact_windows_shows_all_windows_with_tightest_reset():
    out = render_human({"codex": _sample_codex_windows()}, compact=True,
                      use_color=False, use_unicode=False, now=_now())
    assert "AI QUOTAS" not in out
    assert out.count("\n") == 1   # exactly one provider line
    assert "codex" in out
    # Both windows appear, joined with a separator.
    assert "5h 27%" in out
    assert "weekly 59%" in out
    # The 5h window (tighter of the two) contributes the reset delta.
    assert "4h 20m" in out


def test_compact_quota_shows_dollar_used_and_limit():
    from ai_quota.models import Quota
    r = ProviderResult(
        provider="claude", status=STATUS_OK, fetched_at=_now(),
        quotas=(Quota("credits", "usd_credits", 1.66, 250.0, 248.34, 99.336, None),),
        raw={"source": "oauth_live", "source_fetched_at": _now().isoformat(),
             "source_age_seconds": 0, "usage": {}},
    )
    out = render_human({"claude": r}, compact=True, use_color=False, use_unicode=False, now=_now())
    assert "$1.66" in out
    assert "$250.00" in out
    assert "0.7% used" in out


def test_compact_shows_unavailable_reason():
    err = ProviderResult(provider="copilot", status=STATUS_AUTH_ERROR, fetched_at=_now(),
                        error="GitHub authentication is missing")
    out = render_human({"copilot": err}, compact=True, use_color=False, use_unicode=False,
                      now=_now())
    assert "unavailable" in out
    assert "authentication" in out.lower()


def test_compact_omits_source_and_header():
    from ai_quota.models import Quota
    r = ProviderResult(
        provider="claude", status=STATUS_OK, fetched_at=_now(),
        quotas=(Quota("credits", "usd_credits", 1.66, 250.0, 248.34, 99.336, None),),
        raw={"source": "oauth_live", "source_fetched_at": _now().isoformat(),
             "source_age_seconds": 240, "usage": {}},
    )
    out = render_human({"claude": r}, compact=True, use_color=False, use_unicode=False, now=_now())
    assert "source" not in out.lower()
    assert "AI QUOTAS" not in out


# ---------------------------------------------------------------------------
# Statusline (tmux/waybar) format
# ---------------------------------------------------------------------------


def _sample_claude_credits():
    return ProviderResult(
        provider="claude", status=STATUS_OK, fetched_at=_now(),
        quotas=(Quota("credits", "usd_credits", 1.66, 250.0, 248.34, 99.336, None),),
    )


def test_statusline_full_shape():
    results = {
        "codex": _sample_codex_windows(),
        "copilot": _sample_copilot(),
        "claude": _sample_claude_credits(),
    }
    line = render_statusline(results)
    # codex has 5h=27, weekly=59; copilot single quota 213/25000=0.85% (<1%);
    # claude 1.66/250=0.66% (<1%). Verify structure and codex ordering.
    assert line.startswith("|")
    assert line.endswith("|")
    parts = line.strip("|").split("|")
    assert len(parts) == 3
    assert parts[0] == "27%,59%"
    # Codex 5h must come before weekly.
    assert parts[0].index("27") < parts[0].index("59")


def test_statusline_codex_depleted():
    r = ProviderResult(
        provider="codex", status=STATUS_OK, fetched_at=_now(),
        raw={"rateLimits": {"rateLimitReachedType": "workspace_member_credits_depleted",
                            "primary": None, "secondary": None}},
    )
    line = render_statusline({"codex": r, "copilot": _sample_copilot(), "claude": _sample_claude_credits()})
    parts = line.strip("|").split("|")
    assert parts[0] == "!!"


def test_statusline_missing_provider_uses_dash():
    err = ProviderResult(provider="copilot", status=STATUS_AUTH_ERROR, fetched_at=_now(),
                        error="auth")
    line = render_statusline({
        "codex": _sample_codex_windows(),
        "copilot": err,
        "claude": _sample_claude_credits(),
    })
    parts = line.strip("|").split("|")
    assert parts[1] == "-"


def test_statusline_unavailable_provider_is_dash():
    err = ProviderResult(provider="codex", status=STATUS_UNAVAILABLE, fetched_at=_now(),
                        error="not installed")
    line = render_statusline({
        "codex": err,
        "copilot": _sample_copilot(),
        "claude": _sample_claude_credits(),
    })
    parts = line.strip("|").split("|")
    assert parts[0] == "-"


def test_statusline_codex_only_5h_window():
    r = ProviderResult(
        provider="codex", status=STATUS_OK, fetched_at=_now(),
        windows=(UsageWindow("5h", 27.0, 73.0, None, 300),),
    )
    line = render_statusline({"codex": r})
    assert line == "|27%,-|"
