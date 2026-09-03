"""Credential scrubber. Pure. Applied before any debug/log emission."""

import re

_REDACTED = "REDACTED"

# Header-style: "Authorization: Bearer <token>" or "Cookie: <value>".
# Consumes the entire value up to end-of-line (or ; for Cookie).
_HEADER_LINE = re.compile(
    r"(?i)\b(authorization|cookie)(\s*:\s*)([^\r\n;]+)"
)

# "Bearer <token>" anywhere (case-insensitive).
_BEARER = re.compile(r"(?i)\b(bearer)(\s+)(\S+)")

# key=value or key: value for token / api_key / session / api-key.
_KEYVAL = re.compile(
    r"(?i)\b(token|api[_-]?key|session)(\s*[=:]\s*)(\S+)"
)

# GitHub personal/oauth tokens.
_GH_TOKEN = re.compile(r"gh[oprsu]_[A-Za-z0-9]{20,}")

# OpenAI-style secret keys.
_OAI_KEY = re.compile(r"sk-[A-Za-z0-9]{20,}")


def redact(text: str) -> str:
    out = text
    out = _HEADER_LINE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", out)
    out = _BEARER.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", out)
    out = _KEYVAL.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", out)
    out = _GH_TOKEN.sub(_REDACTED, out)
    out = _OAI_KEY.sub(_REDACTED, out)
    return out
