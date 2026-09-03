"""Time utilities. Pure (except local_tz, which only reads env/system config)."""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def local_tz() -> ZoneInfo:
    tz_env = os.environ.get("TZ")
    if tz_env:
        try:
            return ZoneInfo(tz_env)
        except ZoneInfoNotFoundError:
            pass
    try:
        return ZoneInfo("localtime")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def now_local() -> datetime:
    return datetime.now(tz=local_tz())


_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_REL_RE = re.compile(
    r"^(?:in\s+)?"
    r"(?:(?P<d>\d+)d\s*)?"
    r"(?:(?P<h>\d+)h\s*)?"
    r"(?:(?P<m>\d+)m)?$"
)


def _ref(reference: datetime | None) -> datetime:
    return reference if reference is not None else now_local()


def parse_reset(s: str, *, reference: datetime | None = None) -> datetime | None:
    s = s.strip()
    if not s:
        return None

    # ISO-8601 with time
    if _ISO_RE.match(s):
        try:
            iso = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=local_tz())
            return dt
        except ValueError:
            return None

    # Date-only
    if _DATE_RE.match(s):
        try:
            d = datetime.fromisoformat(s)
            return d.replace(tzinfo=local_tz())
        except ValueError:
            return None

    # HH:MM (today, roll to tomorrow if past)
    m = _HHMM_RE.match(s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if not (0 <= hh < 24 and 0 <= mm < 60):
            return None
        ref = _ref(reference)
        candidate = ref.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= ref:
            candidate = candidate + timedelta(days=1)
        return candidate

    # Relative "in Xh Ym" / "Xh Ym" / "Xd"
    m = _REL_RE.match(s)
    if m and any(m.group(k) for k in ("d", "h", "m")):
        days = int(m.group("d") or 0)
        hours = int(m.group("h") or 0)
        mins = int(m.group("m") or 0)
        return _ref(reference) + timedelta(days=days, hours=hours, minutes=mins)

    return None


def humanize_delta(target: datetime, *, now: datetime | None = None) -> str:
    now = now if now is not None else now_local()
    delta = target - now
    total = int(delta.total_seconds())
    if total <= 0:
        return "passed" if total < -60 else "now"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"in {days}d {hours}h"
    if hours:
        return f"in {hours}h {minutes}m"
    return f"in {minutes}m"
