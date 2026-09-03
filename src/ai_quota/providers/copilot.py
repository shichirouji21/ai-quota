"""GitHub Copilot adapter. Uses `gh api /copilot_internal/user`."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from datetime import datetime

from ai_quota.models import (
    STATUS_AUTH_ERROR,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    ProviderResult,
    Quota,
)
from ai_quota.providers.base import Provider, error_result
from ai_quota.timeutils import now_local, parse_reset

_TIMEOUT_S = 10

# Only these top-level fields from `/copilot_internal/user` are safe to keep
# in `raw`. Everything else (login, organization_login_list, organization_list,
# analytics_tracking_id, etc.) identifies the account/org and is dropped.
_RAW_ALLOWLIST = frozenset(
    {
        "copilot_plan",
        "quota_reset_date",
        "quota_reset_date_utc",
        "quota_snapshots",
        "token_based_billing",
        "access_type_sku",
        "chat_enabled",
        "cli_enabled",
    }
)


def _sanitize_raw(body: dict) -> dict:
    return {k: v for k, v in body.items() if k in _RAW_ALLOWLIST}


def _default_transport() -> tuple[int, str, str]:
    proc = subprocess.run(
        ["gh", "api", "/copilot_internal/user", "-H", "Accept: application/json"],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_S,
    )
    return proc.returncode, proc.stdout, proc.stderr


def parse_copilot(body: dict, *, fetched_at: datetime) -> ProviderResult:
    snapshots = body.get("quota_snapshots") or {}
    reset_str = body.get("quota_reset_date_utc") or body.get("quota_reset_date")
    reset_at = parse_reset(reset_str) if reset_str else None

    quotas: list[Quota] = []
    for name, snap in snapshots.items():
        if not snap.get("has_quota"):
            continue
        if snap.get("unlimited"):
            # keep in raw but do not surface as a headline quota
            continue
        entitlement = float(snap.get("entitlement") or 0)
        remaining = float(snap.get("remaining") or 0)
        used = float(snap.get("credits_used") or (entitlement - remaining))
        pct = float(snap.get("percent_remaining") or 0.0)
        unit = "premium_requests" if name == "premium_interactions" else "ai_credits"
        quotas.append(
            Quota(
                name=name,
                unit=unit,
                used=used,
                limit=entitlement,
                remaining=remaining,
                remaining_percent=pct,
                reset_at=reset_at,
            )
        )

    return ProviderResult(
        provider="copilot",
        status=STATUS_OK,
        fetched_at=fetched_at,
        quotas=tuple(quotas),
        raw=_sanitize_raw(body),
    )


def _classify_stderr(stderr: str) -> tuple[str, str]:
    low = stderr.lower()
    if "gh auth login" in low or "not logged" in low or "authentication" in low:
        return STATUS_AUTH_ERROR, "GitHub authentication is missing"
    if "http 401" in low:
        return STATUS_AUTH_ERROR, "HTTP 401: Bad credentials"
    if "http 403" in low:
        return STATUS_AUTH_ERROR, "HTTP 403: no billing permission"
    last = stderr.strip().splitlines()[-1] if stderr.strip() else "unknown gh error"
    return STATUS_ERROR, last


class CopilotProvider(Provider):
    name = "copilot"

    def fetch(self, *, transport: Callable[[], tuple[int, str, str]] | None = None) -> ProviderResult:
        t = transport or _default_transport
        try:
            rc, stdout, stderr = t()
        except FileNotFoundError:
            return error_result(self.name, STATUS_UNAVAILABLE, "`gh` not found in PATH")
        except subprocess.TimeoutExpired:
            return error_result(self.name, STATUS_ERROR, "timeout")
        except Exception as e:  # noqa: BLE001
            return error_result(self.name, STATUS_ERROR, str(e))

        if rc != 0:
            status, err = _classify_stderr(stderr)
            return error_result(self.name, status, err)

        try:
            body = json.loads(stdout)
        except json.JSONDecodeError as e:
            return error_result(self.name, STATUS_ERROR, f"malformed JSON: {e}")

        return parse_copilot(body, fetched_at=now_local())
