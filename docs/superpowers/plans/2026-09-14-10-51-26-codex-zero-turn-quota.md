# Codex Zero-Turn Quota Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace ai-quota's prompt-sending Codex quota probe with the read-only `account/rateLimits/read` app-server RPC so polling can never create a Codex/Astra turn.

**Architecture:** Keep the existing Codex normalization boundary and replace only its transport. A stdlib subprocess client will perform the verified `initialize` → `initialized` → `account/rateLimits/read` sequence over newline-delimited stdio, select response ID 2, and always reap the child; unsupported versions fail closed with no prompt-based fallback.

**Tech Stack:** Python 3.12 stdlib (`subprocess`, `select`, `time`, `json`, `shutil`), pytest, Ruff, Nix.

## Global Constraints

- The production Codex path must never invoke `send-message-v2` or send `noop` or any other prompt.
- The only post-initialization operation is `account/rateLimits/read`; do not start a thread or turn.
- Unsupported Codex versions fail closed; there is no unsafe compatibility fallback.
- Keep the existing `parse_codex_rate_limits(body, fetched_at=...)` schema and normalized output unchanged.
- Keep the existing 15-second provider timeout and 45-second successful-result cache behavior.
- Do not expose or cache initialize responses, stderr, account identifiers beyond the existing sanitized rate-limit result path, or complete raw protocol transcripts.
- Python 3.12 and stdlib-only runtime dependencies remain mandatory.
- Do not modify Copilot, Claude, cache, coordinator, or formatter behavior.
- Create commits only if the implementation session explicitly authorizes commits.

---

## File Structure

- Modify `src/ai_quota/providers/codex.py`: own the read-only app-server protocol, RPC error classification, timeout, cleanup, and existing quota normalization.
- Modify `tests/test_codex.py`: prove the exact request sequence, reject turn-producing operations, cover protocol failures and cleanup, and retain normalization regressions.
- Modify `README.md`: replace the unsafe Codex transport limitation with the direct read-only transport and upgrade/fail-closed behavior.
- Modify `docs/superpowers/specs/2026-09-02-investigation-notes.md`: preserve the old 0.149.0 observation as history while marking the prompt transport unsafe and superseded.

## Timeline Analysis

- **Caller timeline:** spawn child → send initialize → receive response 1 → send initialized → send read request 2 → receive response 2 → normalize result.
- **Child-process timeline:** consume newline-delimited requests and emit responses/notifications independently.
- **Owned state:** one `Popen` instance and one monotonic deadline belong exclusively to one `fetch()` call; no state is shared between concurrent coordinator workers.
- **Required ordering:** request 2 is not sent until response 1 succeeds; unrelated notifications may arrive between matching responses and are ignored.
- **Repetition:** each cache miss starts an independent read-only process; zero, one, repeated, or concurrent refreshes cannot create model work because none sends a thread, turn, or message operation.
- **Failure cleanup:** every exit from the transport runs the same close → terminate → bounded wait → kill/reap sequence.

## Task 1: Replace the Codex transport with the read-only RPC

**Files:**
- Modify: `tests/test_codex.py:1-113`
- Modify: `src/ai_quota/providers/codex.py:1-181`

**Interfaces:**
- Consumes: Codex app-server newline-delimited protocol and existing `parse_codex_rate_limits(body: dict, *, fetched_at: datetime) -> ProviderResult`.
- Produces: `_default_transport() -> dict`, returning only the `account/rateLimits/read` result; `CodexProvider.fetch(*, transport: Callable[[], dict] | None = None) -> ProviderResult`.
- Preserves: existing `ProviderResult` windows/raw schema and status constants.

- [ ] **Step 1: Rewrite Codex tests around a fake stdio app-server process**

Replace `tests/test_codex.py` with the following test module. The fake records every client write and supplies deterministic server lines without invoking Codex:

```python
import io
import json
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


class _RecordingInput(io.StringIO):
    def close(self):
        self.close_requested = True


class _FakeProcess:
    def __init__(self, responses: list[dict | str], *, wait_times_out: bool = False):
        lines = [item if isinstance(item, str) else json.dumps(item) for item in responses]
        self.stdin = _RecordingInput()
        self.stdin.close_requested = False
        self.stdout = io.StringIO("\n".join(lines) + "\n")
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


def _install_fake_process(monkeypatch, process):
    invocation = {}

    def fake_popen(argv, **kwargs):
        invocation["argv"] = argv
        invocation["kwargs"] = kwargs
        return process

    monkeypatch.setattr(codex.shutil, "which", lambda _name: "/bin/codex")
    monkeypatch.setattr(codex.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(codex.select, "select", lambda reads, _writes, _errors, _timeout: (reads, [], []))
    return invocation


def _success_responses():
    return [
        {"id": 1, "result": {"userAgent": "codex"}},
        {"method": "account/rateLimits/updated", "params": {}},
        {"id": 2, "result": _fixture()},
    ]


def test_parse_two_windows_ok():
    r = parse_codex_rate_limits(_fixture(), fetched_at=_now())
    assert r.status == STATUS_OK
    assert {w.name for w in r.windows} == {"5h", "weekly"}
    for window in r.windows:
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
    r = parse_codex_rate_limits(body, fetched_at=_now())
    assert r.status == STATUS_OK
    assert r.windows == ()
    assert r.raw["rateLimits"]["planType"] == "plus"


def test_parse_missing_ratelimits_error():
    assert parse_codex_rate_limits({"unexpected": True}, fetched_at=_now()).status == STATUS_ERROR


def test_default_transport_uses_only_read_only_rpc(monkeypatch):
    process = _FakeProcess(_success_responses())
    invocation = _install_fake_process(monkeypatch, process)

    result = codex._default_transport()

    assert invocation["argv"] == ["codex", "app-server", "--stdio"]
    messages = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
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
    assert result == _fixture()
    assert process.terminated


def test_default_transport_ignores_unrelated_notifications(monkeypatch):
    process = _FakeProcess(_success_responses())
    _install_fake_process(monkeypatch, process)
    assert codex._default_transport()["rateLimits"]["planType"] == "plus"


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
```

- [ ] **Step 2: Run the rewritten Codex tests and verify they fail against the unsafe transport**

Run:

```bash
nix develop --command pytest tests/test_codex.py -q
```

Expected: failures because `ai_quota.providers.codex` does not expose `select`, `CodexRpcError`, or `CodexProtocolError`, the existing transport invokes `debug app-server send-message-v2`, and injected transports currently return debug text rather than result dictionaries.

- [ ] **Step 3: Replace the Codex debug transport with the stdio RPC client**

Replace the module docstring/imports/constants, remove `_NOOP_MESSAGE`, `_BLOCK_RE`, and `_extract_rate_limits_snapshot()`, and add these protocol helpers above `_window_name` in `src/ai_quota/providers/codex.py`:

```python
"""OpenAI Codex adapter using the read-only app-server rate-limit RPC."""

from __future__ import annotations

import json
import select
import shutil
import subprocess
import time
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
_TERMINATE_TIMEOUT_S = 2
_COMMAND = ["codex", "app-server", "--stdio"]


class CodexProtocolError(RuntimeError):
    """The app-server did not complete the expected read-only protocol."""


class CodexRpcError(RuntimeError):
    """The app-server returned an RPC error response."""


def _send(proc: subprocess.Popen, message: dict) -> None:
    if proc.stdin is None:
        raise CodexProtocolError("codex app-server stdin is unavailable")
    proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.flush()


def _read_response(proc: subprocess.Popen, request_id: int, deadline: float) -> dict:
    if proc.stdout is None:
        raise CodexProtocolError("codex app-server stdout is unavailable")

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(_COMMAND, _TIMEOUT_S)
        readable, _, _ = select.select([proc.stdout], [], [], remaining)
        if not readable:
            raise subprocess.TimeoutExpired(_COMMAND, _TIMEOUT_S)

        line = proc.stdout.readline()
        if not line:
            raise CodexProtocolError(f"codex app-server exited before response {request_id}")
        try:
            response = json.loads(line)
        except json.JSONDecodeError as error:
            raise CodexProtocolError("codex app-server emitted malformed JSON") from error
        if response.get("id") == request_id:
            return response


def _response_result(response: dict, operation: str) -> dict:
    error = response.get("error")
    if isinstance(error, dict):
        message = error.get("message") or f"{operation} failed"
        raise CodexRpcError(str(message))
    result = response.get("result")
    if not isinstance(result, dict):
        raise CodexProtocolError(f"{operation} returned no result object")
    return result


def _stop_process(proc: subprocess.Popen) -> None:
    if proc.stdin is not None:
        try:
            proc.stdin.close()
        except OSError:
            pass
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=_TERMINATE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _default_transport() -> dict:
    if not shutil.which("codex"):
        raise FileNotFoundError("codex")

    proc = subprocess.Popen(
        _COMMAND,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    deadline = time.monotonic() + _TIMEOUT_S
    try:
        _send(
            proc,
            {
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "ai-quota", "version": "0.1.0"}},
            },
        )
        _response_result(_read_response(proc, 1, deadline), "initialize")
        _send(proc, {"method": "initialized"})
        _send(proc, {"id": 2, "method": "account/rateLimits/read"})
        return _response_result(_read_response(proc, 2, deadline), "account/rateLimits/read")
    finally:
        _stop_process(proc)
```

Keep `_window_name`, `_extract_window`, and `parse_codex_rate_limits` unchanged. Replace `CodexProvider.fetch` with:

```python
class CodexProvider(Provider):
    name = "codex"

    def fetch(self, *, transport: Callable[[], dict] | None = None) -> ProviderResult:
        t = transport or _default_transport
        try:
            body = t()
        except FileNotFoundError:
            return error_result(self.name, STATUS_UNAVAILABLE, "`codex` not found in PATH")
        except subprocess.TimeoutExpired:
            return error_result(self.name, STATUS_ERROR, "timeout")
        except CodexRpcError as error:
            message = str(error)
            low = message.lower()
            if any(token in low for token in ("not authenticated", "authentication", "unauthorized", "login", "401")):
                return error_result(self.name, STATUS_AUTH_ERROR, "codex authentication required")
            return error_result(self.name, STATUS_ERROR, message)
        except CodexProtocolError as error:
            return error_result(self.name, STATUS_ERROR, str(error))
        except Exception as error:  # noqa: BLE001
            return error_result(self.name, STATUS_ERROR, str(error))

        return parse_codex_rate_limits(body, fetched_at=now_local())
```

- [ ] **Step 4: Run focused tests and make only minimal corrections required by real test output**

Run:

```bash
nix develop --command pytest tests/test_codex.py -q
```

Expected: all Codex tests pass. Do not weaken the request-sequence or forbidden-operation assertions to obtain a pass.

- [ ] **Step 5: Run the complete unit suite and linter**

Run:

```bash
nix develop --command pytest -q
nix develop --command ruff check .
```

Expected: all tests pass (at least the baseline 103 plus the expanded Codex coverage), and Ruff reports `All checks passed!`.

- [ ] **Step 6: Commit the transport fix only when commits were explicitly authorized**

```bash
git add src/ai_quota/providers/codex.py tests/test_codex.py
git commit -m "fix(codex): retrieve quota without creating turns"
```

Expected: one focused code-and-test commit. If commits were not authorized, leave the verified changes uncommitted.

---

## Task 2: Correct transport documentation and verify the packaged fix

**Files:**
- Modify: `README.md:192-207`
- Modify: `docs/superpowers/specs/2026-09-02-investigation-notes.md:7-115,186-196`

**Interfaces:**
- Consumes: the read-only transport implemented in Task 1.
- Produces: user-facing compatibility guidance that never recommends the unsafe debug prompt and historical notes clearly marked as superseded.

- [ ] **Step 1: Replace the README Codex limitation text**

Replace the Codex bullet under `## Provider limitations` with:

```markdown
- **Codex** — uses the app server's read-only stdio protocol: initialize the
  connection, send `initialized`, then call `account/rateLimits/read`. It
  never starts a conversation or sends a prompt, so statusline polling does
  not consume Codex turns. Codex CLI versions that do not provide
  `codex app-server --stdio` and this RPC fail closed as unavailable/error;
  ai-quota never falls back to `send-message-v2`. Upgrade Codex to a current
  release when this occurs. When the workspace is out of credits
  (`workspace_member_credits_depleted`, `workspace_owner_credits_depleted`,
  etc.) the server returns `null` for both the 5-hour and weekly windows —
  the tool then shows a human-readable reason instead of empty progress
  bars. The raw snapshot still exposes `planType`,
  `rateLimitReachedType`, and `credits` under `raw.rateLimits`.
```

- [ ] **Step 2: Mark the old investigation transport as superseded**

In `docs/superpowers/specs/2026-09-02-investigation-notes.md`, replace section `### 1.1 Interface` through its adapter-change list with:

```markdown
### 1.1 Interface (historical finding, superseded 2026-09-14)

Codex CLI 0.149.0 did not expose a working direct stdio path in the tested
NixOS package, leading the original adapter to use
`codex debug app-server send-message-v2 "noop"`. That command sends a real
user prompt. The implemented transport also waited for process completion
before parsing `account/rateLimits/updated`, so it could create a model turn
on every uncached quota refresh.

This transport is unsafe and has been removed. It must not be restored as a
fallback.

Codex CLI 0.153.4 was verified to support:

```text
codex app-server --stdio
```

The current adapter performs `initialize`, sends `initialized`, calls the
read-only `account/rateLimits/read` RPC, and terminates after response ID 2.
Older or incompatible Codex versions fail closed and should be upgraded.
```

At the end of `## 4. Overall reconciliations for later tasks`, replace the Codex bullet with:

```markdown
- **Task 10 (Codex, superseded transport):** camelCase parser findings remain
  valid (`usedPercent`, `resetsAt`, `windowDurationMins`), but the former
  `send-message-v2` transport is unsafe. The production adapter now uses
  direct `codex app-server --stdio` with `account/rateLimits/read` and has no
  prompt-based fallback.
```

- [ ] **Step 3: Assert that production code and current README contain no unsafe fallback**

Run:

```bash
python - <<'PY'
from pathlib import Path

source = Path("src/ai_quota/providers/codex.py").read_text()
readme = Path("README.md").read_text()
assert "send-message-v2" not in source
assert '"noop"' not in source
assert "never falls back to `send-message-v2`" in readme
assert "account/rateLimits/read" in source
print("zero-turn transport assertions passed")
PY
```

Expected: `zero-turn transport assertions passed`.

- [ ] **Step 4: Run a live read-only smoke test**

Run this only with Codex 0.153.4 or newer installed and authenticated:

```bash
nix develop --command python - <<'PY'
from ai_quota.providers.codex import _default_transport

result = _default_transport()
assert isinstance(result.get("rateLimits"), dict)
print("read-only Codex rate-limit RPC succeeded")
PY
```

Expected: `read-only Codex rate-limit RPC succeeded`. The command must not print account IDs, quota values, or raw protocol output, and it must not create a new Codex conversation/thread.

- [ ] **Step 5: Run final repository verification**

Run:

```bash
nix develop --command pytest -q
nix develop --command ruff check .
nix build
git diff --check
git status --short
```

Expected: all tests pass; Ruff reports `All checks passed!`; `nix build` succeeds; `git diff --check` emits no output; status lists only the intended Codex provider, Codex tests, README, historical investigation notes, approved design, and this plan (unless planning docs were already committed separately).

- [ ] **Step 6: Commit documentation only when commits were explicitly authorized**

```bash
git add README.md docs/superpowers/specs/2026-09-02-investigation-notes.md docs/superpowers/specs/2026-09-14-codex-zero-turn-quota-design.md docs/superpowers/plans/2026-09-14-10-51-26-codex-zero-turn-quota.md
git commit -m "docs: document zero-turn Codex quota retrieval"
```

Expected: one focused documentation/plan commit. If commits were not authorized, leave the verified changes uncommitted.

---

## Acceptance Checklist

- [ ] No production code invokes `send-message-v2` or includes a prompt payload.
- [ ] The exact live sequence is `initialize` → `initialized` → `account/rateLimits/read`.
- [ ] Unsupported Codex versions fail closed.
- [ ] Success, error, and timeout paths always reap the app-server process.
- [ ] Existing quota windows and depleted-credit output are unchanged.
- [ ] Focused tests, full pytest, Ruff, Nix build, and live read-only smoke test pass.
- [ ] Current documentation warns against restoring a prompt-based fallback.

## Assumptions

- The execution environment has Codex CLI 0.153.4 or newer for the live smoke test; unit and package tests remain hermetic without it.
- Linux pipe descriptors are compatible with `select.select`, matching ai-quota's documented mainstream Linux target.
- The app server continues to use one JSON object per stdout line and identifies responses with the integer request IDs supplied by the client.
- No version bump or release publication is required unless separately requested.
