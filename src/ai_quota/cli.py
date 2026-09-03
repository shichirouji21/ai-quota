"""argparse CLI, exit-code policy, orchestration."""

from __future__ import annotations

import argparse
import sys

from ai_quota import __version__
from ai_quota.coordinator import PROVIDERS
from ai_quota.coordinator import run as coordinator_run
from ai_quota.formatting import (
    render_human,
    render_json,
    render_statusline,
    should_use_color,
    should_use_unicode,
)
from ai_quota.models import STATUS_OK


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ai-quota",
        description="Unified AI quota CLI for Codex, GitHub Copilot, and Claude.",
    )
    p.add_argument("--json", action="store_true", help="emit JSON")
    p.add_argument("--statusline", action="store_true",
                   help="emit a terse |codex|copilot|claude| line for tmux/waybar")
    p.add_argument("--provider", choices=sorted(PROVIDERS.keys()),
                   help="only query one provider")
    p.add_argument("--no-cache", action="store_true", help="bypass cache")
    p.add_argument("--compact", action="store_true", help="compact human output")
    p.add_argument("--debug", action="store_true",
                   help="print sanitized diagnostics to stderr")
    p.add_argument("--version", action="version", version=f"ai-quota {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    providers = [args.provider] if args.provider else list(PROVIDERS.keys())

    results = coordinator_run(providers=providers, use_cache=not args.no_cache)

    if args.debug:
        for name, r in results.items():
            print(f"[debug] {name}: status={r.status} error={r.error!r}",
                  file=sys.stderr)

    if args.json:
        sys.stdout.write(render_json(results))
        sys.stdout.write("\n")
    elif args.statusline:
        sys.stdout.write(render_statusline(results))
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_human(
            results,
            compact=args.compact,
            use_color=should_use_color(),
            use_unicode=should_use_unicode(),
        ))

    ok_count = sum(1 for r in results.values() if r.status == STATUS_OK)
    return 0 if ok_count > 0 else 1
