# ai-quota — investigation notes (2026-09-02)

Empirical protocol and response findings from the target machine before
adapter implementation. These findings supersede any conflicting
assumption in `2026-09-02-ai-quota-design.md`.

## 1. Codex

**CLI version:** `codex-cli 0.149.0` (from `nixpkgs-unstable`).

### 1.1 Interface

Original plan assumption: `codex app-server --stdio` speaks JSON-RPC on stdin/stdout.

**Reality:** `codex app-server` has NO `--stdio` flag. It is a subcommand
group whose two useful members are `daemon` (start persistent daemon)
and `proxy` (stdio↔socket bridge). **Both require the standalone
installer-managed Codex binary at `~/.codex/packages/standalone/current/codex`.**
The nixpkgs Codex build deliberately does not ship that layout, so on
NixOS the daemon path is unreachable without also running the
`chatgpt.com/codex/install.sh` script outside Nix (which defeats
declarative management).

**Workable Nix-compatible transport:** `codex debug app-server send-message-v2 "<msg>"`.

This subcommand runs the app-server in-process (no daemon required) and
emits an `account/rateLimits/updated` server notification early in the
turn, containing the same `RateLimitSnapshot` shape as the schema
extracted from `generate-json-schema --out`. The adapter parses that
notification's JSON block, extracts `rateLimits`, and kills the process.

Trade-offs of this transport:

- It is a `debug` subcommand, therefore explicitly unstable — likely to
  change or disappear in future Codex releases.
- The `send-message-v2` argument is sent as a user prompt, which would
  normally start a model turn. In practice the rate-limits notification
  arrives before the model call, so we terminate the process on first
  match. In the credit-depleted state (`workspace_member_credits_depleted`)
  the turn fails immediately with no model call. In an active-quota state
  additional care may be needed to avoid consuming a turn — mitigation:
  send a message that yields an immediate short model response, or
  refine the kill timing.

**Adapter change:** the Codex adapter must:

1. Spawn `codex debug app-server send-message-v2 "noop"` with piped
   stdout+stderr (merged).
2. Buffer the output up to a 15 s deadline.
3. Regex-extract every `< { ... }` block, strip the `< ` prefix, parse as JSON.
4. Find the first block whose `method == "account/rateLimits/updated"` and
   pull `params.rateLimits` from it.
5. Terminate the process cleanly.

### 1.2 Handshake

The `initialize` request is the same shape as the plan expected:

    { "jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {
        "clientInfo": { "name": "ai-quota", "version": "0.1.0" }
      } }

`InitializeResponse` has: `codexHome` (path), `platformFamily`,
`platformOs`, `userAgent`.

### 1.3 Rate-limit method

**Method name confirmed: `account/rateLimits/read`** (as the plan assumed).

**Response schema (v2 protocol) — camelCase, not the flat `rate_limits`
array the plan assumed.** The parser must handle this real shape:

    {
      "rateLimits": {
        "planType": "plus",
        "limitId": "codex",
        "limitName": "codex",
        "primary":   { "usedPercent": 27, "resetsAt": <unix_seconds>, "windowDurationMins": 300  },
        "secondary": { "usedPercent": 59, "resetsAt": <unix_seconds>, "windowDurationMins": 10080 },
        "credits": { "hasCredits": bool, "unlimited": bool, "balance": string|null },
        "rateLimitReachedType": null | "rate_limit_reached" | ...,
        "spendControlReached": bool | null
      },
      "rateLimitsByLimitId": { "<limit_id>": <RateLimitSnapshot>, ... } | null,
      "rateLimitResetCredits": {...} | null
    }

    // RateLimitWindow
    {
      "usedPercent": <int>,                        // REQUIRED
      "resetsAt":    <unix seconds> | null,        // optional
      "windowDurationMins": <int> | null           // optional
    }

Two windows to expose: `primary` → `5h` and `secondary` → `weekly`
(inferred from `windowDurationMins` 300 vs 10080). The window names in
the normalized model MUST NOT be hardcoded to "5h"/"weekly" blindly —
derive from `windowDurationMins` when available.

### 1.4 Auth

`account/rateLimits/read` requires an authenticated session (`codex login`
must have completed). This is a manual step; deferred to end-to-end
verification (Task 18).

Auth-failure surfaces as JSON-RPC error object; the plan's mapping still
holds (auth-related error → `STATUS_AUTH_ERROR`).

### 1.5 Fixture

`tests/fixtures/codex/rate_limits_ok.json` was hand-constructed from the
official JSON schema (`codex app-server generate-json-schema --out ...`),
NOT captured from a live authenticated session. This is a documented
compromise; a live capture requires `codex login` first.

## 2. GitHub Copilot

**CLI version:** `gh 2.97.0` (nixpkgs).

**Endpoint:** `gh api /copilot_internal/user` — works as designed.

**Plan type on this machine:** `copilot_plan = "business"`, enterprise
seat.

**Response snapshot (synthetic fixture: `tests/fixtures/copilot/user_business.json`):**

- `login`, `analytics_tracking_id`, org identifiers redacted; numeric quota
  fields replaced with synthetic values that preserve the same schema/shape.
  - `quota_snapshots.chat` — `unlimited=true`, `has_quota=true`, `percent_remaining=100`.
  - `quota_snapshots.completions` — `unlimited=true`, `has_quota=true`, `percent_remaining=100`.
  - `quota_snapshots.premium_interactions` — `unlimited=false`, `entitlement=1000`, `credits_used=123`, `remaining=877`, `percent_remaining=87.7`.
- `quota_reset_date_utc = "2026-10-01T00:00:00.000Z"`.

**Individual-plan variant:** synthetic (`tests/fixtures/copilot/user_individual_ai_credits.json`) — hand-constructed to exercise the `has_quota=true, unlimited=false` code path for the `chat` and `completions` snapshots. Numbers are illustrative.

## 3. Claude

**CLI version:** Claude Code `2.1.223`.

### 3.1 PTY test result

`claude -p "/usage"` was executed both without PTY and under
`pty.fork()`. **Both produce identical, sparse output:**

    You are currently using your subscription to power your Claude Code usage

    What's contributing to your limits usage?
    Approximate, based on local sessions on this machine — does not include other devices or claude.ai. Behaviors are independent characteristics, not a breakdown.

    Last 7d · 241 requests · 3 sessions
      84% of your usage was at >150k context

**No 5-hour window. No weekly window. No reset timestamps.** The full
TUI `/usage` view (with progress bars and reset times) does not render
under `-p` even when a PTY is attached.

The plan's Assumption 3 explicitly covers this fallback: "If Claude Code
detects `-p` and truncates even under PTY, we drop back to the
sparse-summary parser and mark that limitation in the JSON `raw`."

### 3.2 Adapter revision (applies to Task 11)

The Claude adapter must expose only what is available:

- `raw.summary_text` = the sparse text as captured (post ANSI strip).
- Extract when present:
  - `last_7d.requests` (int, e.g. 241)
  - `last_7d.sessions` (int, e.g. 3)
  - `last_7d.high_context_share_percent` (float, e.g. 84.0) — meaning: share of usage at >150k context.
- **Do NOT synthesize `5h`/`weekly` `UsageWindow` objects** — they cannot be sourced from this output.
- `status = "ok"` when at least the "Last 7d · N requests" line parses; `status = "limited"` (new status value alongside existing ones) is NOT introduced — instead `status="ok"` with `raw.limitation = "no window data available from claude -p /usage"` documents the constraint, and the JSON schema for Claude in this build simply has no `windows`/`quotas`.

This is a functional acceptance shortfall vs. spec §Acceptance Criteria
#4 ("real usage information for Claude"). The retrieval is real; the
detail level is what Claude Code exposes non-interactively as of
`2.1.223`. When Claude Code exposes a machine-readable command in a
future release, the adapter's transport layer swaps without touching
the schema.

### 3.3 ANSI

Raw PTY output contains only a trailing `\x1B[?25h` (show-cursor CSI).
Stripped cleanly by `_ANSI_RE`.

## 4. Overall reconciliations for later tasks

- **Task 10 (Codex):** parser input keys are camelCase (`usedPercent`,
  `resetsAt`, `windowDurationMins`). Reset is a Unix seconds integer,
  not an ISO string. Windows come from `primary` and `secondary` fields
  on `rateLimits`, not from a `rate_limits` list. Also, the transport
  uses `codex app-server proxy` (with prior `daemon start`), not
  `codex app-server --stdio`.
- **Task 11 (Claude):** parses only the sparse `Last 7d ...` line and
  the high-context percentage. No windows synthesized.
- **Task 8 tests must be revised to match these actual schemas.**
