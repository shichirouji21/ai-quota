"""Human and JSON formatters. Pure."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from datetime import datetime

from ai_quota.models import ProviderResult, Quota, UsageWindow
from ai_quota.timeutils import humanize_delta, local_tz, now_local


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _window_dict(w: UsageWindow) -> dict:
    return {
        "name": w.name,
        "used_percent": w.used_percent,
        "remaining_percent": w.remaining_percent,
        "reset_at": _iso(w.reset_at),
        "duration_minutes": w.duration_minutes,
    }


def _quota_dict(q: Quota) -> dict:
    return {
        "name": q.name,
        "unit": q.unit,
        "used": q.used,
        "limit": q.limit,
        "remaining": q.remaining,
        "remaining_percent": q.remaining_percent,
        "reset_at": _iso(q.reset_at),
    }


def _provider_dict(r: ProviderResult) -> dict:
    d: dict = {"status": r.status}
    if r.windows:
        d["windows"] = [_window_dict(w) for w in r.windows]
    if r.quotas:
        d["quotas"] = [_quota_dict(q) for q in r.quotas]
        if len(r.quotas) == 1:
            d["quota"] = _quota_dict(r.quotas[0])
    if r.error:
        d["error"] = r.error
    if r.raw is not None:
        d["raw"] = dict(r.raw)
    return d


def render_json(results: Mapping[str, ProviderResult], *, generated_at: datetime | None = None) -> str:
    doc = {
        "generated_at": _iso(generated_at or now_local()),
        "providers": {name: _provider_dict(r) for name, r in results.items()},
    }
    return json.dumps(doc, indent=2, sort_keys=False, default=str)


def progress_bar(percent_used: float, *, width: int = 10, use_unicode: bool = True) -> str:
    used = max(0.0, min(100.0, percent_used))
    filled = int(round((used / 100.0) * width))
    if use_unicode:
        return "█" * filled + "░" * (width - filled)
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _fmt_reset(dt: datetime | None, *, now: datetime | None) -> str:
    if dt is None:
        return "?"
    local = dt.astimezone(local_tz())
    return f"{local.strftime('%H:%M')} ({humanize_delta(local, now=now)})"


def _short_reset(dt: datetime | None, *, now: datetime | None) -> str:
    """Just the delta, e.g. `in 2h 31m` — used by compact rendering."""
    if dt is None:
        return ""
    local = dt.astimezone(local_tz())
    return humanize_delta(local, now=now)


def _compact_window(w) -> str:
    """One window as `5h 27% (in 4h 20m)`. Short names for readability."""
    return f"{w.name} {w.used_percent:.0f}%"


def _compact_line(name: str, r: ProviderResult, *, now: datetime | None) -> str:
    """Return a single-line summary suitable for a status line / tmux."""
    label = f"{name:<8}"

    if r.status != "ok":
        reason = r.error or r.status
        return f"{label} unavailable ({reason})"

    # All windows joined; append the tightest window's reset delta once at the end.
    if r.windows:
        parts = [_compact_window(w) for w in r.windows]
        tightest = min(r.windows, key=lambda w: (w.duration_minutes or 10**9))
        rst = _short_reset(tightest.reset_at, now=now)
        rst_s = f", {rst}" if rst else ""
        return f"{label} " + " · ".join(parts) + rst_s

    # Fall back to the first quota.
    if r.quotas:
        q = r.quotas[0]
        if q.unit.endswith("_credits") and q.unit != "ai_credits":
            symbol = "$" if q.unit.startswith("usd") else ""
            return f"{label} {symbol}{q.used:.2f}/{symbol}{q.limit:.2f} ({100.0 - q.remaining_percent:.1f}% used)"
        return f"{label} {q.used:.0f}/{q.limit:.0f} ({100.0 - q.remaining_percent:.0f}% used)"

    # Ok but no windows/quotas — pull from raw.
    if r.raw and isinstance(r.raw, dict):
        rl = r.raw.get("rateLimits") if isinstance(r.raw.get("rateLimits"), dict) else None
        if rl and rl.get("rateLimitReachedType"):
            return f"{label} {rl['rateLimitReachedType']}"
        l7 = r.raw.get("last_7d")
        if isinstance(l7, dict) and l7.get("requests") is not None:
            return f"{label} last 7d: {l7['requests']} requests, {l7['sessions']} sessions"

    return f"{label} no data"


def _render_compact(
    results: Mapping[str, ProviderResult],
    *,
    now: datetime | None = None,
) -> str:
    return "\n".join(_compact_line(name, r, now=now) for name, r in results.items()) + "\n"


def _statusline_cell(r: ProviderResult) -> str:
    """One provider's cell for status-line rendering. Terse: percentages only.

    Rules:
      - Codex: `<5h>%,<weekly>%` in that order; missing window → `-`.
      - Copilot / Claude quota: `N%` used.
      - Claude with windows (individual plan): tightest window's used%.
      - `!!` when workspace/plan is depleted or rate-limited.
      - `-` when the provider is unavailable / auth failed / no data.
    """
    if r.status != "ok":
        return "-"

    if r.provider == "codex":
        # Two windows (5h, weekly). If either is absent, fill with `-`.
        five_h = next((w for w in r.windows if "5h" in w.name), None)
        weekly = next((w for w in r.windows if "week" in w.name.lower()), None)
        if not r.windows:
            rl = (r.raw or {}).get("rateLimits") if isinstance(r.raw, dict) else None
            if isinstance(rl, dict) and rl.get("rateLimitReachedType"):
                return "!!"
            return "-"
        a = f"{five_h.used_percent:.0f}%" if five_h else "-"
        b = f"{weekly.used_percent:.0f}%" if weekly else "-"
        return f"{a},{b}"

    # Copilot / Claude / anything else: pick the most representative percentage.
    if r.windows:
        w = min(r.windows, key=lambda x: (x.duration_minutes or 10**9))
        return f"{w.used_percent:.0f}%"
    if r.quotas:
        q = r.quotas[0]
        used_pct = 100.0 - q.remaining_percent
        return f"{used_pct:.0f}%"
    return "-"


def render_statusline(
    results: Mapping[str, ProviderResult],
) -> str:
    """Return `|codex|copilot|claude|` terse cells for tmux/waybar."""
    order = ["codex", "copilot", "claude"]
    cells = [_statusline_cell(results[name]) for name in order if name in results]
    # Any provider not in results (e.g. --provider foo) is simply omitted.
    return "|" + "|".join(cells) + "|"


def render_human(
    results: Mapping[str, ProviderResult],
    *,
    compact: bool = False,
    use_color: bool = True,
    use_unicode: bool = True,
    now: datetime | None = None,
) -> str:
    if compact:
        return _render_compact(results, now=now)
    lines: list[str] = ["AI QUOTAS", ""]
    for name, r in results.items():
        title = name.capitalize()
        lines.append(title)
        if r.status != "ok":
            reason = r.error or r.status
            lines.append(f"  unavailable: {reason}")
            lines.append("")
            continue
        for w in r.windows:
            bar = progress_bar(w.used_percent, use_unicode=use_unicode)
            lines.append(
                f"  {w.name:<8}{bar}  {w.used_percent:>5.1f}% used  reset {_fmt_reset(w.reset_at, now=now)}"
            )
        for q in r.quotas:
            bar = progress_bar(100.0 - q.remaining_percent, use_unicode=use_unicode)
            if q.unit.endswith("_credits") and q.unit != "ai_credits":
                # Currency-denominated pool (e.g. usd_credits): show as $X.XX.
                symbol = "$" if q.unit.startswith("usd") else ""
                used_s = f"{symbol}{q.used:.2f}"
                limit_s = f"{symbol}{q.limit:.2f}"
            else:
                used_s = f"{q.used:.0f}"
                limit_s = f"{q.limit:.0f}"
            reset_s = "" if q.reset_at is None else f"   reset {_fmt_reset(q.reset_at, now=now)}"
            lines.append(f"  {q.name:<20}{bar}  {used_s} / {limit_s}{reset_s}")
        # Sparse-summary providers (Claude sparse fallback) may have raw.last_7d
        # but no windows/quotas.
        if not r.windows and not r.quotas and r.raw and isinstance(r.raw, dict):
            l7 = r.raw.get("last_7d")
            if isinstance(l7, dict) and l7.get("requests") is not None:
                lines.append(f"  last 7d   {l7.get('requests')} requests, {l7.get('sessions')} sessions")
                if l7.get("high_context_share_percent") is not None:
                    lines.append(f"            {l7['high_context_share_percent']:.0f}% of usage was at >150k context")
            # Codex: no active windows but a rateLimits snapshot exists.
            rl = r.raw.get("rateLimits") if isinstance(r.raw.get("rateLimits"), dict) else None
            if rl is not None:
                reached = rl.get("rateLimitReachedType")
                plan = rl.get("planType")
                credits = rl.get("credits") or {}
                if reached:
                    human = {
                        "workspace_member_credits_depleted":
                            "workspace out of credits (ask workspace owner to refill)",
                        "workspace_owner_credits_depleted":
                            "workspace out of credits",
                        "workspace_member_usage_limit_reached":
                            "workspace usage limit reached",
                        "workspace_owner_usage_limit_reached":
                            "workspace usage limit reached",
                        "rate_limit_reached": "rate-limited",
                    }.get(reached, reached)
                    lines.append(f"  {human}")
                    lines.append("  (no 5h / weekly window data while limits are exhausted)")
                else:
                    lines.append("  no active rate-limit windows reported")
                if plan:
                    lines.append(f"  plan       {plan}")
                if credits.get("unlimited"):
                    lines.append("  credits    unlimited")
                elif credits.get("balance") is not None:
                    lines.append(f"  credits    {credits['balance']}")
        # Source annotation (Claude cascade): show where the data came from.
        if r.raw and isinstance(r.raw, dict) and r.raw.get("source"):
            src = r.raw["source"]
            age = r.raw.get("source_age_seconds")
            label = {
                "oauth_live": "oauth live",
                "claude_local_cache": "claude local cache",
                "claude_sparse": "claude -p /usage (sparse fallback)",
            }.get(src, src)
            if src == "claude_local_cache" and isinstance(age, (int, float)) and age > 0:
                age_txt = f"{int(age)//60}m {int(age)%60}s old" if age >= 60 else f"{int(age)}s old"
                lines.append(f"  source    {label}, {age_txt}")
            else:
                lines.append(f"  source    {label}")
            if r.raw.get("warning"):
                lines.append(f"  warning   {r.raw['warning']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def should_use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return sys.stdout.isatty()


def should_use_unicode() -> bool:
    if os.environ.get("TERM") == "dumb":
        return False
    return sys.stdout.isatty()
