from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ai_quota.timeutils import humanize_delta, local_tz, parse_reset

CPH = ZoneInfo("Europe/Copenhagen")


def test_local_tz_returns_zoneinfo(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Copenhagen")
    assert isinstance(local_tz(), ZoneInfo)


def test_parse_reset_iso_with_offset():
    r = parse_reset("2026-09-05T12:00:00+02:00")
    assert r == datetime(2026, 9, 5, 12, 0, tzinfo=timezone(timedelta(hours=2)))


def test_parse_reset_iso_zulu():
    r = parse_reset("2026-10-01T00:00:00.000Z")
    assert r is not None
    assert r.tzinfo is not None
    assert r.year == 2026 and r.month == 10 and r.day == 1


def test_parse_reset_date_only(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Copenhagen")
    r = parse_reset("2026-10-01")
    assert r is not None
    assert r.tzinfo is not None
    assert r.year == 2026 and r.month == 10 and r.day == 1
    assert r.hour == 0 and r.minute == 0


def test_parse_reset_hhmm_future_today():
    ref = datetime(2026, 9, 2, 10, 0, tzinfo=CPH)
    r = parse_reset("16:20", reference=ref)
    assert r == datetime(2026, 9, 2, 16, 20, tzinfo=CPH)


def test_parse_reset_hhmm_past_rolls_tomorrow():
    ref = datetime(2026, 9, 2, 18, 0, tzinfo=CPH)
    r = parse_reset("09:00", reference=ref)
    assert r == datetime(2026, 9, 3, 9, 0, tzinfo=CPH)


def test_parse_reset_relative_hm():
    ref = datetime(2026, 9, 2, 10, 0, tzinfo=CPH)
    r = parse_reset("in 2h 30m", reference=ref)
    assert r == datetime(2026, 9, 2, 12, 30, tzinfo=CPH)


def test_parse_reset_invalid():
    assert parse_reset("nonsense") is None


def test_humanize_future():
    now = datetime(2026, 9, 2, 10, 0, tzinfo=CPH)
    tgt = datetime(2026, 9, 2, 12, 31, tzinfo=CPH)
    assert humanize_delta(tgt, now=now) == "in 2h 31m"


def test_humanize_days():
    now = datetime(2026, 9, 2, 10, 0, tzinfo=CPH)
    tgt = datetime(2026, 9, 5, 12, 0, tzinfo=CPH)
    assert humanize_delta(tgt, now=now) == "in 3d 2h"


def test_humanize_past():
    now = datetime(2026, 9, 2, 10, 0, tzinfo=CPH)
    tgt = datetime(2026, 9, 2, 8, 0, tzinfo=CPH)
    assert humanize_delta(tgt, now=now) == "passed"
