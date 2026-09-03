"""Per-provider JSON file cache with TTL. Files chmod 600, dir chmod 700."""

from __future__ import annotations

import contextlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ai_quota.models import ProviderResult, Quota, UsageWindow
from ai_quota.redact import redact


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    d = Path(base) / "ai-quota"
    d.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(d, 0o700)
    return d


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _dt(s: str | None) -> datetime | None:
    if s is None:
        return None
    return datetime.fromisoformat(s)


def _redact_raw(obj: Any) -> Any:
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: _redact_raw(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_raw(v) for v in obj]
    return obj


def to_dict(r: ProviderResult) -> dict:
    return {
        "provider": r.provider,
        "status": r.status,
        "fetched_at": _iso(r.fetched_at),
        "windows": [
            {
                "name": w.name,
                "used_percent": w.used_percent,
                "remaining_percent": w.remaining_percent,
                "reset_at": _iso(w.reset_at),
                "duration_minutes": w.duration_minutes,
            }
            for w in r.windows
        ],
        "quotas": [
            {
                "name": q.name,
                "unit": q.unit,
                "used": q.used,
                "limit": q.limit,
                "remaining": q.remaining,
                "remaining_percent": q.remaining_percent,
                "reset_at": _iso(q.reset_at),
            }
            for q in r.quotas
        ],
        "error": r.error,
        "raw": dict(r.raw) if r.raw is not None else None,
    }


def from_dict(d: dict) -> ProviderResult:
    windows = tuple(
        UsageWindow(
            name=w["name"],
            used_percent=w["used_percent"],
            remaining_percent=w["remaining_percent"],
            reset_at=_dt(w.get("reset_at")),
            duration_minutes=w.get("duration_minutes"),
        )
        for w in d.get("windows", [])
    )
    quotas = tuple(
        Quota(
            name=q["name"],
            unit=q["unit"],
            used=q["used"],
            limit=q["limit"],
            remaining=q["remaining"],
            remaining_percent=q["remaining_percent"],
            reset_at=_dt(q.get("reset_at")),
        )
        for q in d.get("quotas", [])
    )
    return ProviderResult(
        provider=d["provider"],
        status=d["status"],
        fetched_at=_dt(d["fetched_at"]),  # type: ignore[arg-type]
        windows=windows,
        quotas=quotas,
        error=d.get("error"),
        raw=d.get("raw"),
    )


def store(result: ProviderResult) -> None:
    d = to_dict(result)
    if d.get("raw") is not None:
        d["raw"] = _redact_raw(d["raw"])
    path = cache_dir() / f"{result.provider}.json"
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f)


def load(
    provider: str,
    *,
    ttl_seconds: int = 45,
    now: datetime | None = None,
) -> ProviderResult | None:
    path = cache_dir() / f"{provider}.json"
    if not path.exists():
        return None
    try:
        d = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    fetched = _dt(d.get("fetched_at"))
    if fetched is None:
        return None
    now = now if now is not None else datetime.now(tz=UTC)
    if now - fetched > timedelta(seconds=ttl_seconds):
        return None
    try:
        return from_dict(d)
    except (KeyError, ValueError):
        return None
