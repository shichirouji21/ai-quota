# ai-quota — Design Document

**Date:** 2026-09-02
**Source spec:** `~/agent-context/ai-quota-task.md` (canonical requirements)
**Status:** Approved, ready for implementation planning

This document captures the design decisions and empirical findings that
resolve the open questions in the source spec. The source spec remains
authoritative for functional requirements, non-goals, and acceptance
criteria; this document layers concrete choices on top of it.

---

## 1. Goal (restated)

A single local CLI, `ai-quota`, that retrieves and normalizes usage/quota
information for OpenAI Codex, GitHub Copilot, and Anthropic Claude, and
prints either a concise human-readable summary or a stable JSON document.
Optimized for reproducibility, scriptability, and eventual reuse by a
local web dashboard.

---

## 2. Empirical Investigation (performed 2026-09-02)

Real observations from the target machine (NixOS, user `example-user`):

| Tool | Version | Notes |
| --- | --- | --- |
| `codex` | not installed | User uses OpenAI API key inside opencode. Will be installed via `unstable.codex` in home-manager as part of this project. |
| `gh` | 2.97.0 (nixpkgs) | Logged in as `example-user`, keyring-backed, scopes include `read:org`, `repo`, `user`. |
| `claude` | 2.1.223 (Claude Code) | Present at `/etc/profiles/per-user/example-user/bin/claude`. |

### 2.1 GitHub Copilot — clean structured endpoint exists

`gh api /copilot_internal/user` on this business seat returns:

```json
{
  "login": "example-user",
  "copilot_plan": "business",
  "quota_reset_date": "2026-10-01",
  "quota_reset_date_utc": "2026-10-01T00:00:00.000Z",
  "quota_snapshots": {
    "chat":                 {"unlimited": true,  "percent_remaining": 100.0, "credits_used": 0,   "entitlement": 0     },
    "completions":          {"unlimited": true,  "percent_remaining": 100.0, "credits_used": 0,   "entitlement": 0     },
    "premium_interactions": {"unlimited": false, "percent_remaining": 87.7,  "credits_used": 123, "entitlement": 1000, "remaining": 877}
  }
}
```

This is the primary Copilot data source. It is not part of the *public*
REST reference, but it is what `gh copilot` and the official IDE
extensions consume, and it is reachable through authenticated `gh api`
with no extra scopes.

### 2.2 Claude — non-interactive `/usage` is sparse

`claude -p "/usage"` returns only:

```
You are currently using your subscription to power your Claude Code usage

What's contributing to your limits usage?
...
Last 7d · 241 requests · 3 sessions
  84% of your usage was at >150k context
```

No 5h/weekly percentages, no reset timestamps. The full usage view
(current 5-hour session bar, current week bar, reset times, per-model
breakdown) is rendered only in the interactive TUI.

**Decision:** the Claude adapter will invoke `claude -p "/usage"` under a
pseudo-terminal (`pty.openpty()`) so Claude renders the TUI variant into
its stdout buffer. We strip ANSI and parse it tolerantly.

### 2.3 Codex — must be installed

`codex` is absent from `PATH`. The Codex adapter cannot function until
the CLI is installed and `codex login` has been run. As part of this
project we will:

1. Add `unstable.codex` to `home/common/packages.nix` in nixos-config.
2. Document the one-time `codex login` step in the README.
3. Capture the actual `codex app-server` JSON-RPC schema in an
   investigation task before writing the production adapter.

---

## 3. Architecture

### 3.1 Layer stack (strict)

```
argparse CLI               (ai_quota.cli)
      │
JSON/human formatter       (ai_quota.formatting)
      │
Cache (JSON files, 45s)    (ai_quota.cache)
      │
Coordinator                (ai_quota.coordinator)
      │
Provider interface         (ai_quota.providers.base)
      │
┌─────┼─────────────────────┐
Codex   Copilot   Claude    (ai_quota.providers.{codex,copilot,claude})
adapter adapter   adapter
  │       │         │
  │       │         └── subprocess + pty  → ANSI strip → tolerant parser
  │       └────────────── gh api /copilot_internal/user → JSON parse
  └──────────────────────── subprocess (JSON-RPC over stdio) → codex app-server
```

Calculations (percentages, duration labels, reset-relative-time, cache
expiration, progress bar rendering, ANSI stripping, timezone conversion)
are pure functions in their own modules. Actions (subprocess spawn, HTTP
via `gh`, file I/O, stdout write) live in adapter and cache modules.
This separation is a hard rule and is enforced by having pure modules
import nothing from `subprocess`, `os.path`, `sys`, or providers.

### 3.2 Data model (dataclasses, immutable via `frozen=True`)

```python
@dataclass(frozen=True)
class UsageWindow:
    name: str                     # "5h", "weekly", …
    used_percent: float
    remaining_percent: float
    reset_at: datetime | None     # timezone-aware
    duration_minutes: int | None

@dataclass(frozen=True)
class Quota:
    name: str                     # "premium_interactions", …
    unit: str                     # "ai_credits", "premium_requests", …
    used: float
    limit: float
    remaining: float
    remaining_percent: float
    reset_at: datetime | None

@dataclass(frozen=True)
class ProviderResult:
    provider: str                 # "codex" | "copilot" | "claude"
    status: str                   # "ok" | "auth_error" | "unavailable" | "error"
    fetched_at: datetime
    windows: tuple[UsageWindow, ...] = ()
    quotas: tuple[Quota, ...] = ()
    error: str | None = None
    raw: dict | None = None       # provider-specific extras
```

Raw provider dictionaries never propagate into the formatter — the
formatter only ever sees `ProviderResult`.

### 3.3 Concurrency

`concurrent.futures.ThreadPoolExecutor(max_workers=3)` runs the three
provider `fetch()` calls in parallel. Each `fetch()` catches its own
exceptions and returns a `ProviderResult` with `status != "ok"` on
failure — the coordinator never sees an unhandled exception from an
adapter. Per-provider timeout is enforced inside each adapter using
`subprocess.communicate(timeout=…)` or `subprocess.run(timeout=…)`.

Coordinator-level global timeout: 15s. If exceeded, in-flight adapters
are cancelled and produce `status="error", error="timeout"`.

---

## 4. Provider Adapters

### 4.1 Codex

- **Transport:** `subprocess.Popen(["codex", "app-server", "--stdio"], stdin=PIPE, stdout=PIPE, stderr=PIPE, text=True)`.
- **Protocol:** JSON-RPC 2.0, newline-delimited. Send `initialize` with client info, then `account/rateLimits/read`, then `shutdown`, then close stdin. If the process does not exit within 2s, send SIGTERM; after another 2s, SIGKILL.
- **Timeout:** 10s total.
- **Investigation task** (part of the plan) captures the actual handshake and response schema and stores it as a sanitized fixture before the parser is written.
- **Failure modes:** binary missing → `status="unavailable"`; auth failure (`error.code` in known set) → `status="auth_error"`; JSON parse error → `status="error"`; timeout → `status="error", error="timeout"`.

### 4.2 Copilot

- **Transport:** `subprocess.run(["gh", "api", "/copilot_internal/user", "-H", "Accept: application/json"], capture_output=True, text=True, timeout=10)`.
- **Rationale:** reuses existing `gh` auth (keyring), no separate token, no scope changes, no undocumented HTTP client.
- **Normalization:**
  - `quota_snapshots.premium_interactions` → `Quota(name="premium_interactions", unit="premium_requests", used=credits_used, limit=entitlement, remaining=remaining, remaining_percent=percent_remaining, reset_at=quota_reset_date_utc)`.
  - `chat` and `completions` → additional `Quota` entries only when `has_quota` is true; when `unlimited=true` they are attached in `raw` but omitted from human output.
  - `raw` = the entire JSON body (Copilot response contains no secrets — verified against the sample above).
- **Failure modes:** `gh auth status` fails or `gh api` returns 401 → `status="auth_error"`; 403 → `status="auth_error", error="no billing permission"`; missing `quota_snapshots` (older plan) → fallback path yet-TBD; documented as a known limitation.

### 4.3 Claude

- **Transport:** open a pty with `pty.openpty()`, spawn `claude -p "/usage"` with `stdout=slave, stderr=slave, stdin=slave`. Read from the master fd in a loop with a 10s deadline. Terminate the child on read completion or timeout.
- **ANSI stripping:** single compiled regex `re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")`.
- **Parser strategy:** line-oriented, tolerant. Match semantic anchors, not exact strings.
  - Anchor "5-hour" or "5h" case-insensitively → next percentage on the same or following line → `used_percent` (or derive from `remaining` if that's what Claude prints).
  - Anchor "week" case-insensitively → same pattern.
  - Anchor "reset" case-insensitively → parse timestamp (accept ISO-8601, `HH:MM`, `Day HH:MM`, `in Xh Ym`). Convert all forms to timezone-aware ISO-8601 using system TZ.
- **Fixtures** cover: (a) normal output with ANSI, (b) same output with ANSI stripped, (c) missing weekly window, (d) per-model breakdown present, (e) auth-error text, (f) unknown format (parser produces `status="error"`, raw stored).
- **Failure modes:** binary missing → `status="unavailable"`; auth-error line matched → `status="auth_error"`; timeout → `status="error"`; unparseable → `status="error"` with raw preserved.

---

## 5. Caching

- **Location:** `${XDG_CACHE_HOME:-$HOME/.cache}/ai-quota/{codex,copilot,claude}.json`.
- **TTL:** 45 seconds.
- **Semantics:** cache stores serialized `ProviderResult` (no raw auth material — enforced by never caching stderr and only caching bodies from providers whose response is verified to be credential-free). Cache is per-provider so a partial refresh is possible.
- **`--no-cache`** bypasses read and write.
- **File permissions:** `chmod 600` on write.

---

## 6. Time Handling

- All timestamps are `datetime` with `tzinfo`. `zoneinfo.ZoneInfo("localtime")` (falls back to reading `TZ` env or `/etc/localtime`) is used for local display.
- JSON always emits ISO-8601 with offset (e.g., `2026-09-05T12:00:00+02:00`).
- Human display shows both wall-clock (`16:20`) and relative (`in 2h 31m`).
- No fixed CET/CEST offsets. No UTC-only formatting.

---

## 7. CLI Surface

```
ai-quota                       # human summary, all providers, cached
ai-quota --json                # stable JSON schema
ai-quota --provider codex      # one provider (repeatable? no — v1: single value)
ai-quota --debug               # sanitized diagnostics to stderr
ai-quota --no-cache            # force live fetch
ai-quota --compact             # one-line-per-provider variant
ai-quota --version
ai-quota --help
```

**Exit codes:** `0` if ≥1 requested provider produced `status="ok"`; `1`
if all requested providers failed; `2` for argparse errors.

**TTY detection:** color and Unicode progress bars are auto-disabled
when `not sys.stdout.isatty()` or `os.environ.get("TERM") == "dumb"` or
`NO_COLOR` is set.

---

## 8. Security

- Token scrubber `redact(text: str) -> str` applies these regexes before
  any debug/log emission:
  - `(?i)(authorization|bearer|token|api[_-]?key|cookie|session)[=: ]\s*\S+`
  - `gh[oprsu]_[A-Za-z0-9]{20,}` (GitHub tokens)
  - `sk-[A-Za-z0-9]{20,}` (OpenAI keys)
- No tokens are ever passed on argv (only through inherited env / CLI subprocess auth).
- Cache files created with `os.open(..., O_CREAT|O_WRONLY|O_TRUNC, 0o600)`.
- The Copilot response body is inspected during investigation and
  documented as secret-free before it is allowed into cache/raw. The
  Codex `raw` may only include the parsed rate-limit response, not the
  handshake, and not stderr.

---

## 9. Packaging & NixOS Integration

- **Project layout:** as suggested in the source spec (`src/ai_quota/`, `tests/`, `pyproject.toml`, `flake.nix`, `README.md`).
- **Repo:** new git repo at `~/repos/ai-quota`, published later to GitHub.
- **Nix build:** `flake.nix` exposes:
  - `packages.${system}.default = python312Packages.buildPythonApplication { pname = "ai-quota"; version = "0.1.0"; src = ./.; propagatedBuildInputs = []; }` — stdlib-only means no Python deps in the closure.
  - `devShells.${system}.default` with `python312`, `pytest`, `ruff`.
  - `apps.${system}.default = { type = "app"; program = "${self.packages.${system}.default}/bin/ai-quota"; }`.
- **nixos-config wiring:**
  - Add flake input `ai-quota.url = "path:/home/user/repos/ai-quota"` (later flip to `github:…`).
  - Thread through `outputs` and `mkHost` `specialArgs` similar to other local flake inputs.
  - In `home/common/packages.nix`, add `inputs.ai-quota.packages.${pkgs.system}.default` (via `specialArgs`) and also add `unstable.codex`.

---

## 10. Testing Strategy

- **`pytest`**, hermetic, no network, no live provider calls.
- **Fixtures** stored under `tests/fixtures/{codex,copilot,claude}/`; captured during the investigation task with all identifiers redacted.
- **Adapter tests** inject a `Transport` seam: a callable that returns bytes (or raises). Real transport is one implementation; test transport is another. This keeps the parsing code fully unit-testable.
- **Pure-function tests** for: normalization math, ANSI stripper, relative-time formatter, cache TTL check, redact() correctness (including tokens embedded mid-string).
- **Coverage targets** from the source spec §Tests are enumerated as
  concrete test names in the plan.

---

## 11. Non-Goals (echoing spec)

- No browser scraping.
- No cookie extraction.
- No daemon.
- No web UI (JSON is compatible with a future one — that is enough).
- No history DB, no notifications, no tmux/waybar modules in v1.

---

## 12. Assumptions (must hold or plan changes)

1. `unstable.codex` in your nixpkgs-unstable pin exposes `codex app-server --stdio` with a JSON-RPC interface including `account/rateLimits/read`. The investigation task validates this before the parser is written; if it doesn't hold, the Codex task escalates to the user.
2. `gh api /copilot_internal/user` remains reachable with the current `gh` auth. If GitHub deprecates it during implementation, we fall back to the public billing REST API and re-normalize.
3. `claude -p "/usage"` under a PTY produces the same content as the interactive TUI. If Claude Code detects `-p` and truncates even under PTY, we drop back to the sparse-summary parser and mark that limitation in the JSON `raw`.
4. The system timezone is readable via `zoneinfo.ZoneInfo("localtime")` or `/etc/localtime`. On NixOS with a configured `time.timeZone`, both are true.
