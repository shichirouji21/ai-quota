"""OpenAI Codex adapter using the read-only app-server rate-limit RPC."""

from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import time
from collections.abc import Callable
from contextlib import suppress
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
    proc.stdin.write((json.dumps(message) + "\n").encode())
    proc.stdin.flush()


def _read_response(proc: subprocess.Popen, request_id: int, deadline: float, buffer: bytearray) -> dict:
    if proc.stdout is None:
        raise CodexProtocolError("codex app-server stdout is unavailable")

    while True:
        newline = buffer.find(b"\n")
        if newline >= 0:
            line = bytes(buffer[:newline])
            del buffer[: newline + 1]
            if not line:
                continue
            try:
                response = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise CodexProtocolError("codex app-server emitted malformed JSON") from error
            if response.get("id") == request_id:
                return response
            continue

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(_COMMAND, _TIMEOUT_S)
        readable, _, _ = select.select([proc.stdout], [], [], remaining)
        if not readable:
            raise subprocess.TimeoutExpired(_COMMAND, _TIMEOUT_S)
        chunk = os.read(proc.stdout.fileno(), 4096)
        if not chunk:
            raise CodexProtocolError(f"codex app-server exited before response {request_id}")
        buffer.extend(chunk)


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
        with suppress(OSError):
            proc.stdin.close()
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
        bufsize=0,
    )
    deadline = time.monotonic() + _TIMEOUT_S
    buffer = bytearray()
    try:
        _send(
            proc,
            {
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "ai-quota", "version": "0.1.0"}},
            },
        )
        _response_result(_read_response(proc, 1, deadline, buffer), "initialize")
        _send(proc, {"method": "initialized"})
        _send(proc, {"id": 2, "method": "account/rateLimits/read"})
        result = _response_result(_read_response(proc, 2, deadline, buffer), "account/rateLimits/read")
        return {"rateLimits": result.get("rateLimits")}
    finally:
        _stop_process(proc)


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
