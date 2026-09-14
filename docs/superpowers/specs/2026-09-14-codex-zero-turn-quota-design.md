# Codex Zero-Turn Quota Retrieval Design

**Date:** 2026-09-14
**Status:** Approved for implementation planning
**Investigation:** `/home/ada/repos/agent-context/handoffs/ai-quota-astra-turns-investigation.md`

## Goal

Ensure every Codex quota refresh is a read-only operation that cannot create a conversation, model inference, or billable Codex/Astra turn.

## Root Cause

The current Codex transport launches:

```text
codex debug app-server send-message-v2 noop
```

`send-message-v2` submits `noop` as a real user message. The transport then calls `proc.communicate(timeout=15)` and does not inspect `account/rateLimits/updated` until the process has completed or timed out. It therefore cannot terminate before the model turn starts, despite comments claiming that it does.

With a 15-second tmux status interval and a 45-second successful-result cache, this can create approximately one real Codex turn per minute. Failed results are not cached and can retry every 15 seconds.

## Verified Read-Only Protocol

Codex CLI 0.153.4 was verified locally to support a direct stdio app server:

```text
codex app-server --stdio
```

The following newline-delimited protocol returned the expected quota response without starting a thread or sending a user message:

1. Request `initialize` with `clientInfo.name` and `clientInfo.version`.
2. Wait for the initialize response.
3. Send the `initialized` notification.
4. Request `account/rateLimits/read` with request ID 2 and no params.
5. Read until response ID 2 arrives.

The generated Codex 0.153.4 JSON schema confirms that `account/rateLimits/read` is a supported client request and that its result is `GetAccountRateLimitsResponse`. The live response contained `rateLimits`, `rateLimitsByLimitId`, `rateLimitResetCredits`, `rateLimitUpsell`, and `accountId`; no sensitive values were printed during verification.

## Design

### Transport

Replace the debug-command transport in `src/ai_quota/providers/codex.py` with a focused stdio transport that:

- resolves `codex` through `shutil.which`;
- starts `codex app-server --stdio` with piped stdin and stdout;
- performs the verified initialize/initialized handshake;
- sends only `account/rateLimits/read`;
- parses newline-delimited JSON responses as they arrive;
- returns only the `result` object associated with request ID 2;
- enforces the existing finite timeout; and
- always terminates and reaps the child process, escalating from terminate to kill when necessary.

The production transport must contain no prompt text and invoke no command or RPC whose name contains `message`, `thread/start`, or `turn/start`.

### Provider and parser boundary

`CodexProvider.fetch()` will consume the rate-limit result dictionary directly instead of parsing line-prefixed debug output. `parse_codex_rate_limits()` remains responsible only for normalization and continues to accept the existing `{"rateLimits": ...}` schema. The window and depleted-credit behavior therefore remain unchanged.

The debug-output block regex and `_extract_rate_limits_snapshot()` become dead code and will be removed.

### Fail-closed compatibility

There is no fallback to `codex debug app-server send-message-v2` under any condition.

- Missing `codex` reports `status="unavailable"`.
- An unavailable or unsupported `app-server --stdio` command reports a concise provider failure and recommends upgrading Codex where the process output supports that diagnosis.
- Authentication-related RPC errors report `status="auth_error"`.
- Timeout, malformed JSON, premature process exit, missing response ID 2, and other protocol failures report `status="error"`.

Failed provider results remain uncached under the existing coordinator policy. This no longer risks creating turns because retries use only the read-only RPC.

### Security

Only the ID-2 `result` enters the existing normalized `ProviderResult.raw` path. Initialize responses, unrelated notifications, stderr, and protocol chatter are not cached. Error text exposed by the provider remains concise and must not include complete raw server output.

## Testing

Implementation follows test-driven development. Codex tests will first be rewritten around a fake app-server process/transport and will verify:

- the exact request order: `initialize`, `initialized`, `account/rateLimits/read`;
- the quota request has ID 2 and no mutation-oriented params;
- no invocation or payload contains `send-message-v2`, `noop`, `thread/start`, `turn/start`, or another user-message operation;
- the response with ID 2 is selected while unrelated notifications are ignored;
- the existing two-window and depleted-credit fixtures normalize unchanged;
- RPC authentication errors map to `auth_error`;
- malformed JSON, premature exit, missing response, and timeout map to `error`;
- the process is terminated and reaped on success and failure; and
- a missing binary remains `unavailable`.

Repository verification will run:

```text
nix develop --command pytest -q
nix develop --command ruff check .
nix build
```

A final live smoke test may perform only the verified initialize/initialized/rateLimits-read sequence. It will assert response shape without printing account identifiers or quota values. Analytics should be checked after repeated polling when practical, but absence of a turn-producing request in the complete production path is the deterministic acceptance criterion.

## Documentation

Update `README.md` to describe direct read-only app-server retrieval and fail-closed compatibility. Correct `docs/superpowers/specs/2026-09-02-investigation-notes.md` so its historical findings do not present the unsafe prompt transport as the current adapter design. Retain the historical Codex-version context where useful, clearly marked as superseded.

## Non-Goals

- No changes to Copilot or Claude providers.
- No cache TTL or tmux polling changes.
- No daemon/proxy dependency.
- No prompt-based compatibility fallback.
- No unrelated refactoring.

## Assumptions

- Codex versions intended for continued support expose `codex app-server --stdio` and `account/rateLimits/read`; unsupported versions fail closed.
- The current camelCase `GetAccountRateLimitsResponse` schema remains the parser contract.
- `ai-quota` remains targeted at Python 3.12 on mainstream Linux, so a stdlib subprocess-based transport is appropriate.
