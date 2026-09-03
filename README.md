# ai-quota

Unified local CLI that reports usage/quota for OpenAI Codex, GitHub Copilot,
and Anthropic Claude, using existing authenticated CLIs — no browser
scraping, no cookie extraction, no persisted credentials.

## Usage

```
ai-quota                 # human summary
ai-quota --json          # stable JSON
ai-quota --compact       # one line per provider (readable)
ai-quota --statusline    # terse |codex|copilot|claude| for tmux / waybar
ai-quota --provider codex
ai-quota --provider copilot
ai-quota --provider claude
ai-quota --no-cache      # bypass cache
ai-quota --debug         # sanitized diagnostics on stderr
```

Exit codes: `0` if at least one provider succeeded, `1` if all requested
providers failed, `2` for CLI-argument errors.

### Output examples

Full (default):

    AI QUOTAS

    Codex
      5h      ░░░░░░░░░░    0.0% used  reset 19:31 (in 4h 59m)
      weekly  ░░░░░░░░░░    0.0% used  reset 14:31 (in 6d 23h)

    Copilot
      premium_interactions██░░░░░░░░  123 / 1000   reset 00:00 (in 28d 9h)

    Claude
      credits             ░░░░░░░░░░  $12.34 / $500.00
      source    oauth live

Compact:

    codex    5h 0% · weekly 0%, in 4h 59m
    copilot  123/1000 (12% used)
    claude   $12.34/$500.00 (2.5% used)

Statusline (single line, no trailing newline in shell substitution):

    |0%,0%|17%|1%|

- **Codex cell** — `<5h>%,<weekly>%` in that order. `-` for a missing
  window. `!!` when the workspace is depleted or rate-limited.
- **Copilot / Claude cell** — single `N%` (tightest window if any,
  otherwise first quota's used %).
- **Any cell `-`** — provider is unavailable / auth failed / no data.

### Integrating with tmux

Drop this into your tmux config:

    set -g status-interval 15
    set -g status-right-length 100
    set -g status-right "#(ai-quota --statusline 2>/dev/null)  #h  %a %b %e %H:%M"

Refresh every 15 s. ai-quota's own 45 s TTL cache absorbs the polling
so provider CLIs are not called more than needed. Warm-cache calls are
typically <100 ms.

## Installation

`ai-quota` is pure Python, `>=3.12`, with zero third-party runtime
dependencies, so it builds as a universal `py3-none-any` wheel — any
mainstream Linux distribution works.

### pipx (recommended for most Linux users)

```bash
pipx install git+https://github.com/shichirouji21/ai-quota.git
```

### uv

```bash
uv tool install git+https://github.com/shichirouji21/ai-quota.git
```

### Arch Linux

```bash
sudo pacman -S python-pipx github-cli
pipx ensurepath
pipx install git+https://github.com/shichirouji21/ai-quota.git
```

(`uv` from the `extra` repo works the same way: `sudo pacman -S uv`.)

### Nix / NixOS

Standalone:

```bash
nix run github:shichirouji21/ai-quota
```

As a flake input to your system configuration:

```nix
inputs.ai-quota.url = "github:shichirouji21/ai-quota";
```

Thread through `outputs` / `mkHost` `specialArgs`, then in
`home/common/packages.nix`:

```nix
{ pkgs, unstable, ai-quota, ... }:
{
  home.packages = [
    ai-quota.packages.${pkgs.system}.default
    unstable.codex          # for the Codex adapter
  ];
}
```

### From source

```bash
git clone https://github.com/shichirouji21/ai-quota.git
cd ai-quota
python -m venv .venv
source .venv/bin/activate
pip install .
```

For development (pulls in `pytest`, `ruff`, `build`):

```bash
pip install -e '.[dev]'
```

## Provider prerequisites

`ai-quota` reuses the authentication of whichever provider CLIs are already
installed on your machine — it does not implement its own login flow.
Providers are independent: if one is unavailable, the others still work.

| Provider | Requirement |
| --- | --- |
| OpenAI Codex | Codex CLI installed and signed in (`codex login`) |
| GitHub Copilot | GitHub CLI installed and authenticated (`gh auth login`) |
| Claude | Claude Code installed and authenticated |

## Authentication

`ai-quota` never stores, modifies, logs, or passes credentials through
command-line arguments. It reuses existing authenticated CLI sessions:

- `gh auth status` for Copilot (business seat verified against
  `/copilot_internal/user`).
- `codex login` for Codex (one-time, browser flow, run manually after
  installing the CLI).
- The active `claude` session for Claude Code. The Claude adapter reads
  Claude Code's OAuth access token in-process (see [Security](#security))
  to perform its read-only usage request.

If a provider is not authenticated, it reports `auth_error` or
`unavailable`. The tool still returns success (exit 0) as long as at
least one other provider succeeded.

## JSON schema

```json
{
  "generated_at": "2026-09-02T13:45:00+02:00",
  "providers": {
    "codex":   { "status": "ok", "windows": [ ... ], "raw": { ... } },
    "copilot": { "status": "ok", "quotas": [ ... ], "quota": { ... }, "raw": { ... } },
    "claude":  { "status": "ok", "raw": { "last_7d": { ... }, "limitation": "..." } }
  }
}
```

Failed providers use `status` in `{ "auth_error", "unavailable", "error" }`
and populate `error` with a short reason. See
`docs/superpowers/specs/2026-09-02-ai-quota-design.md` for the full model.

## Cache

Results are cached for 45 s under `$XDG_CACHE_HOME/ai-quota/` (files
`chmod 600`, dir `chmod 700`). Cached responses pass through the credential
redactor as defense in depth. `--no-cache` bypasses the cache.

## Provider limitations

- **Codex** — the documented `codex app-server proxy` transport requires
  the *standalone installer-managed* Codex binary (installed via
  `chatgpt.com/codex/install.sh`), which is not what nixpkgs ships. The
  adapter therefore uses `codex debug app-server send-message-v2 "noop"`,
  which runs the app-server in-process on any Codex build and emits an
  `account/rateLimits/updated` notification. This is a `debug`
  subcommand — likely to change in future Codex releases; when it does,
  only the transport layer swaps. The schema and parser stay stable.
  When the workspace is out of credits (`workspace_member_credits_depleted`,
  `workspace_owner_credits_depleted`, etc.) the server returns `null`
  for both the 5-hour and weekly windows — the tool then shows a
  human-readable reason instead of empty progress bars. The raw
  snapshot still exposes `planType`, `rateLimitReachedType`, and
  `credits` under `raw.rateLimits`.
- **Copilot** — uses `gh api /copilot_internal/user`. This endpoint is
  undocumented but is what the official IDE extensions consume. Business
  seats often mark `chat` and `completions` as `unlimited`; those are
  kept in `raw` but not surfaced in the headline view. If GitHub removes
  the endpoint, the adapter's stderr classifier surfaces the error text.
- **Claude** — sources a live quota snapshot from a three-step cascade:
    1. **Live:** `GET https://api.anthropic.com/api/oauth/usage` using the
       OAuth token Claude Code already stores at
       `~/.claude/.credentials.json`. This is an internal Anthropic
       endpoint (unstable) — the adapter isolates it and never rotates
       or refreshes the token itself.
    2. **Local structured cache:** `~/.claude.json → cachedUsageUtilization`
       (the same schema Claude Code populates for its own UI).
    3. **Sparse fallback:** `claude -p /usage` under a PTY (last-7d
       requests/sessions only — no window data).

  Individual/Pro subscriptions populate `five_hour`, `seven_day` (and
  model-specific variants like `seven_day_sonnet`) → rendered as
  standard progress-bar windows. Team/Business seats often have those
  fields `null` and instead expose a dollar-denominated `spend` /
  `extra_usage` pool → rendered as a `usd_credits` `Quota`
  (`$X.XX / $Y.YY`).

  The active source and its age appear under each Claude section
  (`source oauth live` or `source claude local cache, 4m old`). If the
  live endpoint 429s, the cache is used and a `warning` field is
  attached. If none of the three sources succeed, Claude reports
  `unavailable`.

  Security: the OAuth token is read in-process (never passed as an argv
  Bearer flag), never written to the cache, and never emitted in debug
  output. `accountUuid` is stripped from the sanitized `raw.usage`
  copy before it enters any cache file.

## Troubleshooting

- `ai-quota --debug` prints per-provider `status`/`error` on stderr
  (redacted).
- `unavailable: codex not found in PATH` → install the Codex CLI (e.g.
  `unstable.codex` in home-manager).
- `auth_error` → run the vendor CLI's login (`gh auth login`,
  `codex login`, or restart `claude`).
- Slow first run? The cache is cold. Warm-cache runs are typically
  <100 ms.

## Security

`ai-quota` does not implement its own provider login flow and does not
persist provider credentials. It reuses existing authenticated CLI sessions.

For Claude, the tool reads Claude Code's OAuth access token in-process to
perform the read-only usage request. The token is never logged, printed,
passed as a command-line argument, or stored in the ai-quota cache.

- No tokens on argv.
- No credentials written to cache.
- Debug output goes through a token/cookie/gh-token/OpenAI-key scrubber.
- `~/.cache/ai-quota/*.json` created with `chmod 600`.
- Copilot's raw `/copilot_internal/user` response is filtered through an
  allowlist before being surfaced in `--json`, dropping `login`,
  organization identifiers, and analytics tracking IDs.

## Development

```bash
nix develop
pytest -q
ruff check .
```

Fixtures live under `tests/fixtures/`. Adapter tests use a `transport`
injection point so nothing hits the network.

## License

MIT. See `LICENSE`.
