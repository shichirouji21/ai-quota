"""Claude Code adapter.

Source cascade (in fetch order):

    1. Live: HTTPS GET https://api.anthropic.com/api/oauth/usage
       Bearer-authenticated with the OAuth token Claude Code already stores at
       ~/.claude/.credentials.json (never rotated or refreshed by this tool).
       This is an internal/undocumented Anthropic endpoint — treat schema
       and beta header as unstable and isolate everything behind this
       adapter.

    2. Local structured cache: ~/.claude.json → cachedUsageUtilization.
       Populated by Claude Code itself. Same schema as the OAuth body.

    3. Sparse historical summary: `claude -p "/usage"` under a PTY. Only
       used when the two structured sources are unavailable. Produces
       last-7d requests/sessions/high-context share; no window data.

The normalized `ProviderResult` carries:

    windows[]    5h, weekly, and (when reported) model-specific windows
    quotas[]     dollar-denominated pool ("extra_usage" / "spend")
                 present on Team/Business seats without window quotas
    raw          {source, source_fetched_at, source_age_seconds, usage, warning?}

Security invariants:

    - The OAuth access token is never emitted to argv (urllib is used
      in-process; there is no `curl -H` invocation).
    - The token is never written to the cache (only the response body is
      preserved, and the body does not contain the token).
    - accountUuid is stripped from raw before it enters the cache.
    - This adapter never rotates or refreshes the OAuth token. If the
      live request returns 401 the adapter falls back to the local cache
      and reports a warning. The next Claude Code session refreshes its
      own credentials.

See `docs/superpowers/specs/2026-09-02-investigation-notes.md` §3 for the
empirical derivation. See `~/agent-context/claude-quota-research.md` for
the source cascade rationale.
"""

from __future__ import annotations

import contextlib
import json
import os
import pty
import re
import select
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ai_quota.ansi import strip_ansi
from ai_quota.models import (
    STATUS_AUTH_ERROR,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    ProviderResult,
    Quota,
    make_window,
)
from ai_quota.providers.base import Provider, error_result
from ai_quota.timeutils import now_local, parse_reset

_OAUTH_URL = "https://api.anthropic.com/api/oauth/usage"
_OAUTH_BETA = "oauth-2025-04-20"
_HTTP_TIMEOUT_S = 10.0
_SPARSE_TIMEOUT_S = 15
_CREDS_PATH = Path.home() / ".claude" / ".credentials.json"
_LOCAL_CACHE_PATH = Path.home() / ".claude.json"

# Semantic name → durations, used when normalizing an OAuth/cache payload.
_WINDOW_DURATIONS = {
    "five_hour": ("5h", 300),
    "seven_day": ("weekly", 10080),
    "seven_day_opus": ("weekly (opus)", 10080),
    "seven_day_sonnet": ("weekly (sonnet)", 10080),
    "seven_day_oauth_apps": ("weekly (oauth apps)", 10080),
}


# ---------------------------------------------------------------------------
# Source #1: live OAuth endpoint (urllib, in-process, no subprocess).
# ---------------------------------------------------------------------------


def _read_access_token() -> str | None:
    try:
        creds = json.loads(_CREDS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    tok = (creds.get("claudeAiOauth") or {}).get("accessToken")
    return tok if isinstance(tok, str) and tok else None


def _claude_ua() -> str:
    """Best-effort `claude-code/<version>` string, used only in the UA header."""
    try:
        proc = subprocess.run(
            ["claude", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        first = (proc.stdout or "").split()[0] if proc.stdout else ""
        if first:
            return f"claude-code/{first}"
    except (OSError, subprocess.SubprocessError):
        pass
    return "claude-code/unknown"


def _fetch_oauth_usage(
    token: str,
    user_agent: str,
    *,
    timeout: float = _HTTP_TIMEOUT_S,
) -> tuple[int, dict | None, str | None]:
    """Return (http_status, parsed_body, error_message)."""
    req = urllib.request.Request(_OAUTH_URL, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("anthropic-beta", _OAUTH_BETA)
    req.add_header("User-Agent", user_agent)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(body), None
            except json.JSONDecodeError as e:
                return resp.status, None, f"malformed JSON: {e}"
    except urllib.error.HTTPError as e:
        return e.code, None, e.reason or f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return 0, None, str(e.reason)
    except TimeoutError:
        return 0, None, "timeout"


# ---------------------------------------------------------------------------
# Source #2: ~/.claude.json cachedUsageUtilization.
# ---------------------------------------------------------------------------


def _read_local_cache() -> tuple[dict, datetime] | None:
    try:
        outer = json.loads(_LOCAL_CACHE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    cached = outer.get("cachedUsageUtilization")
    if not isinstance(cached, dict):
        return None
    utilization = cached.get("utilization")
    if not isinstance(utilization, dict):
        return None
    fetched_ms = cached.get("fetchedAtMs")
    if isinstance(fetched_ms, (int, float)):
        try:
            fetched_at = datetime.fromtimestamp(int(fetched_ms) / 1000.0, tz=UTC)
        except (OverflowError, OSError, ValueError):
            fetched_at = datetime.now(tz=UTC)
    else:
        fetched_at = datetime.now(tz=UTC)
    return utilization, fetched_at


# ---------------------------------------------------------------------------
# Payload → normalized windows/quotas (same shape for OAuth and local cache).
# ---------------------------------------------------------------------------


def _extract_windows(payload: dict) -> list:
    windows = []
    for key, (name, duration) in _WINDOW_DURATIONS.items():
        entry = payload.get(key)
        if not isinstance(entry, dict):
            continue
        util = entry.get("utilization")
        if util is None:
            continue
        reset_str = entry.get("resets_at")
        reset_dt = parse_reset(reset_str) if isinstance(reset_str, str) else None
        windows.append(
            make_window(
                name=name,
                used_percent=float(util),
                reset_at=reset_dt,
                duration_minutes=duration,
            )
        )
    return windows


def _extract_dollar_quota(payload: dict) -> Quota | None:
    """extra_usage / spend both report a dollar-denominated pool. Prefer
    `spend` because it carries the richer schema (`amount_minor` in cents)."""
    spend = payload.get("spend")
    if isinstance(spend, dict) and isinstance(spend.get("used"), dict) \
            and isinstance(spend.get("limit"), dict):
        used_minor = spend["used"].get("amount_minor")
        limit_minor = spend["limit"].get("amount_minor")
        currency = spend["used"].get("currency") or "USD"
        percent = spend.get("percent")
        if isinstance(used_minor, (int, float)) and isinstance(limit_minor, (int, float)) \
                and limit_minor > 0:
            used = float(used_minor) / 100.0
            limit = float(limit_minor) / 100.0
            remaining = max(0.0, limit - used)
            remaining_pct = 100.0 - (float(percent) if isinstance(percent, (int, float)) else (used / limit * 100.0))
            return Quota(
                name="credits",
                unit=f"{currency.lower()}_credits",
                used=used,
                limit=limit,
                remaining=remaining,
                remaining_percent=max(0.0, min(100.0, remaining_pct)),
                reset_at=None,
            )

    extra = payload.get("extra_usage")
    if isinstance(extra, dict) and extra.get("is_enabled") and extra.get("monthly_limit"):
        # Values are in minor units (cents).
        used_minor = extra.get("used_credits")
        limit_minor = extra.get("monthly_limit")
        currency = extra.get("currency") or "USD"
        util_pct = extra.get("utilization")
        if isinstance(used_minor, (int, float)) and isinstance(limit_minor, (int, float)) \
                and limit_minor > 0:
            used = float(used_minor) / 100.0
            limit = float(limit_minor) / 100.0
            remaining = max(0.0, limit - used)
            if isinstance(util_pct, (int, float)):
                remaining_pct = max(0.0, 100.0 - float(util_pct))
            else:
                remaining_pct = max(0.0, 100.0 - (used / limit * 100.0))
            return Quota(
                name="credits",
                unit=f"{currency.lower()}_credits",
                used=used,
                limit=limit,
                remaining=remaining,
                remaining_percent=remaining_pct,
                reset_at=None,
            )
    return None


def _sanitize_payload(payload: dict) -> dict:
    """Strip account identifiers before the payload enters raw/cache."""
    def scrub(obj):
        if isinstance(obj, dict):
            return {k: ("REDACTED" if k == "accountUuid" else scrub(v)) for k, v in obj.items()}
        if isinstance(obj, list):
            return [scrub(v) for v in obj]
        return obj
    return scrub(payload)


def _build_result(
    payload: dict,
    *,
    source: str,
    source_fetched_at: datetime,
    fetched_at: datetime,
    warning: str | None = None,
) -> ProviderResult:
    windows = tuple(_extract_windows(payload))
    quota = _extract_dollar_quota(payload)
    quotas = (quota,) if quota is not None else ()

    age = max(0.0, (fetched_at - source_fetched_at).total_seconds())
    raw = {
        "source": source,
        "source_fetched_at": source_fetched_at.isoformat(),
        "source_age_seconds": int(age),
        "usage": _sanitize_payload(payload),
    }
    if warning:
        raw["warning"] = warning

    return ProviderResult(
        provider="claude",
        status=STATUS_OK,
        fetched_at=fetched_at,
        windows=windows,
        quotas=quotas,
        raw=raw,
    )


# ---------------------------------------------------------------------------
# Source #3: sparse `claude -p /usage` fallback (kept for last-resort use).
# ---------------------------------------------------------------------------


_LABEL_AUTH = re.compile(r"(?i)claude\s+login|not\s+logged\s+in|please\s+authenticate")
_SUMMARY_RE = re.compile(
    r"(?i)Last\s+7d\s*[·•]\s*(\d+)\s+requests?\s*[·•]\s*(\d+)\s+sessions?"
)
_HIGH_CONTEXT_RE = re.compile(
    r"(\d{1,3}(?:\.\d+)?)\s*%\s+of\s+your\s+usage\s+was\s+at\s*>"
)


def parse_claude_usage(text: str, *, fetched_at: datetime) -> ProviderResult:
    """Parse the sparse `claude -p /usage` output. Kept for the final fallback."""
    if _LABEL_AUTH.search(text):
        return error_result("claude", STATUS_AUTH_ERROR, "claude authentication required")

    summary = _SUMMARY_RE.search(text)
    if not summary:
        return ProviderResult(
            provider="claude",
            status=STATUS_ERROR,
            fetched_at=fetched_at,
            error="unable to parse /usage output",
            raw={"source": "claude_sparse", "text": text},
        )

    requests = int(summary.group(1))
    sessions = int(summary.group(2))
    hc = _HIGH_CONTEXT_RE.search(text)
    high_context = float(hc.group(1)) if hc else None

    raw = {
        "source": "claude_sparse",
        "source_fetched_at": fetched_at.isoformat(),
        "source_age_seconds": 0,
        "text": text,
        "last_7d": {
            "requests": requests,
            "sessions": sessions,
            "high_context_share_percent": high_context,
        },
        "limitation": (
            "claude -p /usage exposes only a sparse summary; upgrade to a Claude Code "
            "version that populates the OAuth /api/oauth/usage endpoint or the local "
            "cachedUsageUtilization for full window data"
        ),
    }
    return ProviderResult(
        provider="claude",
        status=STATUS_OK,
        fetched_at=fetched_at,
        raw=raw,
    )


def _sparse_transport() -> tuple[int, str]:
    """PTY-wrapped `claude -p /usage`."""
    if not shutil.which("claude"):
        raise FileNotFoundError("claude")

    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.execvp("claude", ["claude", "-p", "/usage"])
        except OSError:
            os._exit(127)

    deadline = time.monotonic() + _SPARSE_TIMEOUT_S
    buf = bytearray()
    exit_status = 0
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired("claude", _SPARSE_TIMEOUT_S)
            r, _, _ = select.select([fd], [], [], min(0.5, remaining))
            if not r:
                try:
                    wpid, status = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    break
                if wpid == pid:
                    exit_status = os.waitstatus_to_exitcode(status)
                    break
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf.extend(chunk)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
        try:
            wpid, status = os.waitpid(pid, os.WNOHANG)
            if wpid == 0:
                os.kill(pid, 15)
                os.waitpid(pid, 0)
        except (ChildProcessError, ProcessLookupError):
            pass

    return exit_status, buf.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Cascade orchestration.
# ---------------------------------------------------------------------------


Source = Callable[[], "ProviderResult | object | None"]


def _try_oauth() -> ProviderResult | None:
    token = _read_access_token()
    if token is None:
        return None
    ua = _claude_ua()
    status, body, err = _fetch_oauth_usage(token, ua)
    now = now_local()
    if status == 200 and isinstance(body, dict):
        return _build_result(body, source="oauth_live", source_fetched_at=now, fetched_at=now)
    if status in (401, 403):
        # Expired / revoked; let the cascade fall through to local cache.
        return None
    if status == 429:
        # Signal rate-limit so the next source can warn about it.
        return _RATE_LIMITED
    if status >= 500 or status == 0:
        return None
    # Any other status: give up on this source silently, cascade continues.
    return None


# Sentinel returned by _try_oauth when the endpoint 429s. Any truthy
# non-ProviderResult value would do; we use a named singleton so
# `is _RATE_LIMITED` reads clearly at the call site.
_RATE_LIMITED = object()


def _try_local_cache(*, warning: str | None = None) -> ProviderResult | None:
    got = _read_local_cache()
    if got is None:
        return None
    payload, source_fetched_at = got
    return _build_result(
        payload,
        source="claude_local_cache",
        source_fetched_at=source_fetched_at,
        fetched_at=now_local(),
        warning=warning,
    )


def _try_sparse() -> ProviderResult | None:
    try:
        _rc, raw = _sparse_transport()
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return None
    except Exception:  # noqa: BLE001
        return None
    plain = strip_ansi(raw)
    result = parse_claude_usage(plain, fetched_at=now_local())
    if result.status != STATUS_OK:
        return None
    return result


class ClaudeProvider(Provider):
    name = "claude"

    def fetch(
        self,
        *,
        transport: Callable[[], tuple[int, str]] | None = None,
        sources: list[Source] | None = None,
    ) -> ProviderResult:
        # Back-compat: if a caller injects `transport`, use it as the sparse source.
        if transport is not None and sources is None:
            try:
                _rc, raw = transport()
            except FileNotFoundError:
                return error_result(self.name, STATUS_UNAVAILABLE, "`claude` not found in PATH")
            except subprocess.TimeoutExpired:
                return error_result(self.name, STATUS_ERROR, "timeout")
            except Exception as e:  # noqa: BLE001
                return error_result(self.name, STATUS_ERROR, str(e))
            return parse_claude_usage(strip_ansi(raw), fetched_at=now_local())

        if sources is None:
            sources = [_try_oauth, _try_local_cache, _try_sparse]

        warning: str | None = None
        for src in sources:
            try:
                result = src()
            except Exception as e:  # noqa: BLE001
                # A misbehaving source must not sink the cascade.
                warning = warning or f"source error: {e}"
                continue
            if result is None:
                continue
            if result is _RATE_LIMITED:
                warning = "live Claude quota endpoint rate-limited"
                continue
            if warning is not None and result.raw is not None:
                # Attach the warning to whichever source ultimately succeeded.
                new_raw = dict(result.raw)
                new_raw["warning"] = warning
                result = ProviderResult(
                    provider=result.provider,
                    status=result.status,
                    fetched_at=result.fetched_at,
                    windows=result.windows,
                    quotas=result.quotas,
                    error=result.error,
                    raw=new_raw,
                )
            return result

        return error_result(
            self.name,
            STATUS_UNAVAILABLE,
            "no Claude quota source available (OAuth endpoint, local cache, and /usage all failed)",
        )
