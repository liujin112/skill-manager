# Interactive terminal selectors (arrow-key single/multi pickers) used when the
# CLI runs on a TTY without enough arguments. Ported from the original skillctl.
from __future__ import annotations

import os
import select
import shutil
import sys
import termios
import tty
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

from .registry import CLIError


@dataclass(frozen=True)
class Choice:
    label: str
    value: object
    enabled: bool = True
    hint: str = ""


def is_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def read_key() -> str:
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = os.read(fd, 1)
        if ch == b"\x03":
            return "quit"
        if ch in {b"\r", b"\n"}:
            return "enter"
        if ch == b" ":
            return "space"
        if ch in {b"q", b"Q"}:
            return "quit"
        if ch in {b"j", b"J"}:
            return "down"
        if ch in {b"k", b"K"}:
            return "up"
        if ch in {b"a", b"A"}:
            return "all"
        if ch == b"\x1b":
            seq = b""
            while select.select([sys.stdin], [], [], 0.02)[0]:
                seq += os.read(fd, 1)
                if len(seq) >= 8:
                    break
            if seq.startswith(b"[A"):
                return "up"
            if seq.startswith(b"[B"):
                return "down"
            if seq.startswith(b"[H") or seq.startswith(b"OH"):
                return "home"
            if seq.startswith(b"[F") or seq.startswith(b"OF"):
                return "end"
            return "escape"
        return ch.decode(errors="ignore")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _render(title: str, choices: Sequence[Choice], cursor: int, selected: Optional[set]) -> None:
    height = max(8, shutil.get_terminal_size((80, 24)).lines)
    window = max(5, height - 7)
    start = max(0, min(cursor - window // 2, len(choices) - window))
    end = min(len(choices), start + window)
    sys.stdout.write("\x1b[2J\x1b[H\x1b[?25l")
    sys.stdout.write(title + "\n")
    if selected is None:
        sys.stdout.write("↑/↓ or k/j: move    Enter/Space: confirm    q: cancel\n\n")
    else:
        sys.stdout.write("↑/↓ or k/j: move    Space: select    a: all/none    Enter: confirm    q: cancel\n\n")
    if start > 0:
        sys.stdout.write(f"  ... {start} more above\n")
    for idx in range(start, end):
        choice = choices[idx]
        pointer = "➤" if idx == cursor else " "
        mark = " " if selected is None else ("✓" if idx in selected else " ")
        disabled = " (unavailable)" if not choice.enabled else ""
        hint = f"  {choice.hint}" if choice.hint else ""
        line = f"{pointer} [{mark}] {choice.label}{disabled}{hint}"
        if not choice.enabled:
            line = "\x1b[2m" + line + "\x1b[0m"
        sys.stdout.write(line + "\n")
    if end < len(choices):
        sys.stdout.write(f"  ... {len(choices) - end} more below\n")
    sys.stdout.flush()


def _first_enabled(choices: Sequence[Choice]) -> int:
    for idx, choice in enumerate(choices):
        if choice.enabled:
            return idx
    return 0


def _next_enabled(choices: Sequence[Choice], cursor: int, step: int) -> int:
    idx = cursor
    for _ in range(len(choices)):
        idx = (idx + step) % len(choices)
        if choices[idx].enabled:
            return idx
    return cursor


def select_one(title: str, choices: Sequence[Choice]) -> Choice:
    if not any(choice.enabled for choice in choices):
        raise CLIError("nothing selectable")
    cursor = _first_enabled(choices)
    try:
        while True:
            _render(title, choices, cursor, None)
            key = read_key()
            if key == "up":
                cursor = _next_enabled(choices, cursor, -1)
            elif key == "down":
                cursor = _next_enabled(choices, cursor, 1)
            elif key == "home":
                cursor = _first_enabled(choices)
            elif key == "end":
                for idx in range(len(choices) - 1, -1, -1):
                    if choices[idx].enabled:
                        cursor = idx
                        break
            elif key in {"enter", "space"} and choices[cursor].enabled:
                return choices[cursor]
            elif key in {"quit", "escape"}:
                raise KeyboardInterrupt
    except KeyboardInterrupt:
        sys.stdout.write("\x1b[?25h\nCancelled.\n")
        raise SystemExit(130)
    finally:
        sys.stdout.write("\x1b[?25h")
        sys.stdout.flush()


def select_many(title: str, choices: Sequence[Choice]) -> List[Choice]:
    enabled = [idx for idx, choice in enumerate(choices) if choice.enabled]
    if not enabled:
        raise CLIError("nothing selectable")
    cursor = enabled[0]
    selected: set = set()
    try:
        while True:
            _render(title, choices, cursor, selected)
            key = read_key()
            if key == "up":
                cursor = _next_enabled(choices, cursor, -1)
            elif key == "down":
                cursor = _next_enabled(choices, cursor, 1)
            elif key == "space" and choices[cursor].enabled:
                selected.symmetric_difference_update({cursor})
            elif key == "all":
                enabled_set = set(enabled)
                selected = set() if selected == enabled_set else enabled_set
            elif key == "enter":
                if not selected and choices[cursor].enabled:
                    selected = {cursor}  # nothing ticked: confirm the highlighted item
                if selected:
                    return [choices[idx] for idx in sorted(selected)]
            elif key in {"quit", "escape"}:
                raise KeyboardInterrupt
    except KeyboardInterrupt:
        sys.stdout.write("\x1b[?25h\nCancelled.\n")
        raise SystemExit(130)
    finally:
        sys.stdout.write("\x1b[?25h")
        sys.stdout.flush()


def prompt_path(prompt: str, must_exist: bool = False) -> Path:
    value = input(prompt).strip()
    if not value:
        raise CLIError("empty path")
    path = Path(value).expanduser().resolve()
    if must_exist and not path.exists():
        raise CLIError(f"path not found: {path}")
    return path
