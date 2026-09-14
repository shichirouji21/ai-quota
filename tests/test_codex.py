import io
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_quota.models import (
    STATUS_AUTH_ERROR,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_UNAVAILABLE,
)
from ai_quota.providers import codex
from ai_quota.providers.codex import CodexProvider, parse_codex_rate_limits

FIXTURES = Path(__file__).parent / "fixtures" / "codex"


def _now():
    return datetime(2026, 9, 2, 10, 0, tzinfo=UTC)


def _fixture():
    return json.loads((FIXTURES / "rate_limits_ok.json").read_text())


class _RecordingInput:
    def __init__(self):
        self._parts = []
        self.close_requested = False

    def write(self, value):
        self._parts.append(value)

    def flush(self):
        pass

    def close(self):
        self.close_requested = True

    def text(self):
        return b"".join(part if isinstance(part, bytes) else part.encode() for part in self._parts).decode()


class _FakeOutput(io.StringIO):
    def fileno(self):
        return 0


class _FakeProcess:
    def __init__(self, responses: list[dict | str], *, wait_times_out: bool = False):
        lines = [item if isinstance(item, str) else json.dumps(item) for item in responses]
        self.stdin = _RecordingInput()
        self.stdout = _FakeOutput("\n".join(lines) + ("\n" if lines else ""))
        self.returncode = None
        self.terminated = False
        self.killed = False
        self._wait_times_out = wait_times_out

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        if self._wait_times_out and not self.killed:
            raise subprocess.TimeoutExpired("codex app-server --stdio", timeout)
        self.returncode = self.returncode if self.returncode is not None else 0
        return self.returncode


def _install_fake_process(monkeypatch, process, *, fake_select=True, fake_read=True):
    invocation = {}

    def fake_popen(argv, **kwargs):
        invocation["argv"] = argv
        invocation["kwargs"] = kwargs
        return process

    monkeypatch.setattr(codex.shutil, "which", lambda _name: "/bin/codex")
    monkeypatch.setattr(codex.subprocess, "Popen", fake_popen)
    if fake_select:
        monkeypatch.setattr(codex.select, "select", lambda reads, _writes, _errors, _timeout: (reads, [], []))
    if fake_read:
        monkeypatch.setattr(codex.os, "read", lambda _fd, _size: process.stdout.readline().encode())
    return invocation


def _success_responses():
    return [
        {"id": 1, "result": {"userAgent": "codex"}},
        {"method": "account/rateLimits/updated", "params": {}},
        {"id": 2, "result": {**_fixture(), "accountId": "account-identifier"}},
    ]


def test_parse_two_windows_ok():
    result = parse_codex_rate_limits(_fixture(), fetched_at=_now())
    assert result.status == STATUS_OK
    assert {window.name for window in result.windows} == {"5h", "weekly"}
    for window in result.windows:
        assert 0.0 <= window.used_percent <= 100.0
        assert window.remaining_percent == 100.0 - window.used_percent
        assert window.reset_at is not None
        assert window.reset_at.tzinfo is not None


def test_parse_null_windows_still_ok():
    body = {
        "rateLimits": {
            "primary": None,
            "secondary": None,
            "planType": "plus",
            "rateLimitReachedType": None,
        }
    }
    result = parse_codex_rate_limits(body, fetched_at=_now())
    assert result.status == STATUS_OK
    assert result.windows == ()
    assert result.raw["rateLimits"]["planType"] == "plus"


def test_parse_missing_ratelimits_error():
    assert parse_codex_rate_limits({"unexpected": True}, fetched_at=_now()).status == STATUS_ERROR


def test_default_transport_uses_only_read_only_rpc(monkeypatch):
    process = _FakeProcess(_success_responses())
    invocation = _install_fake_process(monkeypatch, process)

    result = codex._default_transport()

    assert invocation["argv"] == ["codex", "app-server", "--stdio"]
    messages = [json.loads(line) for line in process.stdin.text().splitlines()]
    assert [message["method"] for message in messages] == [
        "initialize",
        "initialized",
        "account/rateLimits/read",
    ]
    assert messages[0]["id"] == 1
    assert messages[1] == {"method": "initialized"}
    assert messages[2] == {"id": 2, "method": "account/rateLimits/read"}
    serialized = json.dumps({"argv": invocation["argv"], "messages": messages})
    for forbidden in ("send-message-v2", "noop", "thread/start", "turn/start"):
        assert forbidden not in serialized
    assert result == {"rateLimits": _fixture()["rateLimits"]}
    assert process.terminated


def test_default_transport_ignores_unrelated_notifications(monkeypatch):
    process = _FakeProcess(_success_responses())
    _install_fake_process(monkeypatch, process)
    assert codex._default_transport()["rateLimits"]["planType"] == "plus"


def test_default_transport_reads_response_already_buffered_by_text_stdout(monkeypatch):
    read_fd, write_fd = os.pipe()
    writer = os.fdopen(write_fd, "wb", buffering=0)
    payload = b"\n".join(json.dumps(response).encode() for response in _success_responses()) + b"\n"
    writer.write(payload)

    process = _FakeProcess([])
    process.stdout = io.TextIOWrapper(os.fdopen(read_fd, "rb", buffering=0))
    _install_fake_process(monkeypatch, process, fake_select=False, fake_read=False)
    monkeypatch.setattr(codex, "_TIMEOUT_S", 0.01)
    try:
        assert codex._default_transport()["rateLimits"]["planType"] == "plus"
    finally:
        writer.close()
        process.stdout.close()


def test_fetch_binary_missing():
    def transport():
        raise FileNotFoundError("codex")

    assert CodexProvider().fetch(transport=transport).status == STATUS_UNAVAILABLE


def test_fetch_auth_error_from_rpc():
    def transport():
        raise codex.CodexRpcError("not authenticated; run codex login")

    assert CodexProvider().fetch(transport=transport).status == STATUS_AUTH_ERROR


def test_fetch_non_auth_rpc_error():
    def transport():
        raise codex.CodexRpcError("method not found")

    result = CodexProvider().fetch(transport=transport)
    assert result.status == STATUS_ERROR
    assert "method not found" in (result.error or "")


def test_fetch_malformed_result_error():
    def transport():
        return {"unexpected": True}

    assert CodexProvider().fetch(transport=transport).status == STATUS_ERROR


def test_default_transport_rejects_malformed_json(monkeypatch):
    process = _FakeProcess(["not-json"])
    _install_fake_process(monkeypatch, process)
    with pytest.raises(codex.CodexProtocolError, match="malformed JSON"):
        codex._default_transport()
    assert process.terminated


def test_default_transport_rejects_premature_exit(monkeypatch):
    process = _FakeProcess([])
    process.returncode = 2
    _install_fake_process(monkeypatch, process)
    with pytest.raises(codex.CodexProtocolError, match="before response 1"):
        codex._default_transport()


def test_default_transport_timeout_terminates_process(monkeypatch):
    process = _FakeProcess([], wait_times_out=True)
    _install_fake_process(monkeypatch, process)
    monkeypatch.setattr(codex.select, "select", lambda *_args: ([], [], []))
    with pytest.raises(subprocess.TimeoutExpired):
        codex._default_transport()
    assert process.terminated
    assert process.killed


def test_fetch_ok_stub():
    result = CodexProvider().fetch(transport=_fixture)
    assert result.status == STATUS_OK
    assert len(result.windows) == 2


def test_fetch_ok_null_windows():
    def transport():
        return {
            "rateLimits": {
                "primary": None,
                "secondary": None,
                "planType": None,
                "rateLimitReachedType": "workspace_member_credits_depleted",
                "credits": {"hasCredits": False, "unlimited": False, "balance": None},
            }
        }

    result = CodexProvider().fetch(transport=transport)
    assert result.status == STATUS_OK
    assert result.windows == ()
    assert result.raw["rateLimits"]["rateLimitReachedType"] == "workspace_member_credits_depleted"
