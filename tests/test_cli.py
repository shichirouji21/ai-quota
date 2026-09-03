import json
from datetime import UTC

import pytest

from ai_quota.cli import main
from ai_quota.models import STATUS_ERROR, STATUS_OK, ProviderResult


def _stub_run(status: str):
    from datetime import datetime

    def run(*, providers, use_cache, ttl_seconds=45, global_timeout_s=15.0):
        return {
            name: ProviderResult(provider=name, status=status,
                                 fetched_at=datetime.now(UTC),
                                 error=None if status == "ok" else "boom")
            for name in providers
        }
    return run


def test_exit_zero_when_any_ok(monkeypatch, capsys):
    monkeypatch.setattr("ai_quota.cli.coordinator_run", _stub_run(STATUS_OK))
    rc = main(["--provider", "copilot"])
    assert rc == 0


def test_exit_one_when_all_fail(monkeypatch, capsys):
    monkeypatch.setattr("ai_quota.cli.coordinator_run", _stub_run(STATUS_ERROR))
    rc = main([])
    assert rc == 1


def test_json_output_is_valid_json(monkeypatch, capsys):
    monkeypatch.setattr("ai_quota.cli.coordinator_run", _stub_run(STATUS_OK))
    rc = main(["--json", "--provider", "copilot"])
    out = capsys.readouterr().out
    doc = json.loads(out)
    assert "providers" in doc
    assert rc == 0


def test_invalid_provider_exits_two(monkeypatch):
    with pytest.raises(SystemExit) as e:
        main(["--provider", "unknown"])
    assert e.value.code == 2
