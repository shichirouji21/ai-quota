"""ANSI escape stripping. Pure."""

import re

_ANSI_RE = re.compile(r"\x1B(?:[0-9@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)
