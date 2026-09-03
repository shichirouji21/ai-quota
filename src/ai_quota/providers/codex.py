"""OpenAI Codex adapter.

**Transport note (Codex CLI 0.147.0+ on NixOS):**

The documented `codex app-server proxy` requires the daemon started via
`codex app-server daemon start`, which in turn requires the *standalone
installer-managed* Codex binary at `~/.codex/packages/standalone/current/`.
The nixpkgs Codex build deliberately does not ship that layout, so the
daemon path is unreachable on a purely-declarative NixOS system.

The transport therefore uses `codex debug app-server send-message-v2 ...`,
which runs the app-server in-process and emits an
`account/rateLimits/updated` server notification early in the turn. The
adapter extracts the `rateLimits` snapshot from that notification and
kills the process. This is an unstable `debug` subcommand and may change
or disappear in future Codex releases; when it does, only the transport
layer here needs to swap. The parser (`parse_codex_rate_limits`) works
against the stable schema.

See `docs/superpowers/specs/2026-09-02-investigation-notes.md` §1 for the
full derivation.
"""

from __future__ import annotations

import json
import re
import shutil
import signal
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime

from ai_quota.models import (
    STATUS_AUTH_ERROR,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    ProviderResult,
    make_window,
)
from ai_quota.providers.base import Provider, error_result
from ai_quota.timeutils import now_local

_TIMEOUT_S = 15
_NOOP_MESSAGE = "noop"


def _default_transport() -> str:
    """Return the raw stdout+stderr text of a short codex debug turn."""
    if not shutil.which("codex"):
        raise FileNotFoundError("codex")

    proc = subprocess.Popen(
        ["codex", "debug", "app-server", "send-message-v2", _NOOP_MESSAGE],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        stdout, _ = proc.communicate(timeout=_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.send_signal(signal.SIGTERM)
        try:
            stdout, _ = proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, _ = proc.communicate()
    return stdout or ""


# `codex debug` prints JSON blocks with each line prefixed by `< ` (server
# → client) or `> ` (client → server). Find the outer server block(s):
# opening `< {` on one line, matching `< }` at column 0 on a later line.
_BLOCK_RE = re.compile(r"^< (\{.*?^< \})", re.DOTALL | re.MULTILINE)


def _extract_rate_limits_snapshot(text: str) -> dict | None:
    for m in _BLOCK_RE.finditer(text):
        raw = m.group(1)
        stripped = "\n".join(
            line[2:] if line.startswith("< ") else line
            for line in raw.splitlines()
        )
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if obj.get("method") == "account/rateLimits/updated":
            params = obj.get("params") or {}
            rl = params.get("rateLimits")
            if isinstance(rl, dict):
                return {"rateLimits": rl}
    return None


def _window_name(duration_mins: int | None, slot: str) -> str:
    if duration_mins is None:
        return {"primary": "5h", "secondary": "weekly"}.get(slot, slot)
    if duration_mins <= 60 * 12:
        return "5h"
    if duration_mins <= 60 * 24:
        return "daily"
    if duration_mins <= 60 * 24 * 8:
        return "weekly"
    return "monthly"


def _extract_window(w: dict | None, slot: str):
    if not isinstance(w, dict):
        return None
    used = w.get("usedPercent")
    if used is None:
        return None
    resets_at = w.get("resetsAt")
    duration = w.get("windowDurationMins")
    reset_dt = None
    if isinstance(resets_at, (int, float)):
        try:
            reset_dt = datetime.fromtimestamp(int(resets_at), tz=UTC)
        except (OverflowError, OSError, ValueError):
            reset_dt = None
    return make_window(
        name=_window_name(duration, slot),
        used_percent=float(used),
        reset_at=reset_dt,
        duration_minutes=int(duration) if isinstance(duration, (int, float)) else None,
    )


def parse_codex_rate_limits(body: dict, *, fetched_at: datetime) -> ProviderResult:
    snapshot = body.get("rateLimits")
    if not isinstance(snapshot, dict):
        return error_result("codex", STATUS_ERROR, "unexpected schema: no rateLimits object")

    windows = []
    for slot in ("primary", "secondary"):
        w = _extract_window(snapshot.get(slot), slot)
        if w is not None:
            windows.append(w)

    # Even without active windows the snapshot is meaningful (planType,
    # rateLimitReachedType, credits). status=ok with empty windows tuple;
    # raw carries the full snapshot for the JSON consumer.
    return ProviderResult(
        provider="codex",
        status=STATUS_OK,
        fetched_at=fetched_at,
        windows=tuple(windows),
        raw=body,
    )


class CodexProvider(Provider):
    name = "codex"

    def fetch(self, *, transport: Callable[[], str] | None = None) -> ProviderResult:
        t = transport or _default_transport
        try:
            raw_text = t()
        except FileNotFoundError:
            return error_result(self.name, STATUS_UNAVAILABLE, "`codex` not found in PATH")
        except subprocess.TimeoutExpired:
            return error_result(self.name, STATUS_ERROR, "timeout")
        except Exception as e:  # noqa: BLE001
            return error_result(self.name, STATUS_ERROR, str(e))

        low = raw_text.lower()
        if "not authenticated" in low or "please run `codex login`" in low or "run codex login" in low:
            return error_result(self.name, STATUS_AUTH_ERROR, "codex authentication required")

        body = _extract_rate_limits_snapshot(raw_text)
        if body is None:
            return error_result(
                self.name,
                STATUS_ERROR,
                "no account/rateLimits/updated notification found in codex output",
            )

        return parse_codex_rate_limits(body, fetched_at=now_local())
