# ANSI color helpers. Colors turn off automatically when stdout is not a TTY
# or the NO_COLOR environment variable is set.
from __future__ import annotations

import os
import sys


def _enabled() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


ENABLED = _enabled()


def _wrap(code: str):
    def paint(text: object) -> str:
        if not ENABLED:
            return str(text)
        return f"\x1b[{code}m{text}\x1b[0m"

    return paint


bold = _wrap("1")
dim = _wrap("2")
red = _wrap("31")
green = _wrap("32")
yellow = _wrap("33")
cyan = _wrap("36")
