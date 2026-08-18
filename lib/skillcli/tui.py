# Interactive terminal selectors in the clack style (npx skills / create-app):
# prompts redraw in place inside a small window instead of clearing the screen,
# collapse to a single summary line once answered, and support live filtering
# with `/` (some imported repos carry 30+ skills).
from __future__ import annotations

import os
import re
import select
import shutil
import sys
import termios
import tty
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .colors import ENABLED as COLORS, bold, cyan, dim
from .registry import CLIError

MAX_ROWS = 12  # list window height; the frame never grows past this + chrome

GLYPH_ACTIVE = "◆"
GLYPH_DONE = "◇"
GLYPH_BAR = "│"
GLYPH_END = "└"


@dataclass(frozen=True)
class Choice:
    label: str
    value: object
    enabled: bool = True
    hint: str = ""


def is_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


# ---------------------------------------------------------------------------
# key input

def _read_utf8(fd: int, first: bytes) -> str:
    """Finish reading a UTF-8 sequence whose first byte is already in hand."""
    b = first[0]
    need = 1 if 0xC0 <= b <= 0xDF else 2 if 0xE0 <= b <= 0xEF else 3 if 0xF0 <= b <= 0xF7 else 0
    seq = first
    while need and select.select([sys.stdin], [], [], 0.05)[0]:
        seq += os.read(fd, 1)
        need -= 1
    return seq.decode(errors="ignore")


def read_key(chars: bool = False) -> str:
    """Read one keypress. Navigation keys come back as names ('up', 'enter',
    'space', ...). With chars=True, printable characters (including j/k/a/q
    and multi-byte UTF-8) are returned literally for text entry."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        # TCSADRAIN, not setraw's default TCSAFLUSH: FLUSH discards keys that
        # arrived between read_key calls, dropping input typed at paste speed.
        tty.setraw(fd, termios.TCSADRAIN)
        ch = os.read(fd, 1)
        if ch == b"\x03":
            return "quit"
        if ch in {b"\r", b"\n"}:
            return "enter"
        if ch in {b"\x7f", b"\x08"}:
            return "backspace"
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
        if not chars:
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
            if ch == b"/":
                return "filter"
        if ch[0] >= 0x80:
            return _read_utf8(fd, ch)
        text = ch.decode(errors="ignore")
        return text if text.isprintable() or text == " " else ""
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ---------------------------------------------------------------------------
# width-aware truncation (descriptions may contain CJK, which is double-width;
# an overflowing line would wrap and break the in-place redraw accounting)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _cwidth(ch: str) -> int:
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _wtrunc(text: str, width: int) -> str:
    text = " ".join(text.split())
    used = 0
    for idx, ch in enumerate(text):
        used += _cwidth(ch)
        if used > width:
            return text[: max(0, idx - 1)] + "…"
    return text


# ---------------------------------------------------------------------------
# in-place frame

class _Frame:
    """Redraws a block of lines in place: move the cursor back up over the
    previous frame, overwrite, and clear whatever is left below."""

    def __init__(self) -> None:
        self.height = 0

    def draw(self, lines: Sequence[str]) -> None:
        out = []
        if self.height:
            out.append(f"\x1b[{self.height}F")
        for line in lines:
            out.append("\x1b[2K" + line + "\n")
        out.append("\x1b[J")
        sys.stdout.write("".join(out))
        sys.stdout.flush()
        self.height = len(lines)

    def clear(self) -> None:
        if self.height:
            sys.stdout.write(f"\x1b[{self.height}F\x1b[J")
            sys.stdout.flush()
            self.height = 0


def step_done(title: str, value: str = "") -> None:
    """Print the one-line collapsed summary of an answered prompt."""
    line = f"{dim(GLYPH_DONE)} {title}"
    if value:
        line += dim(" · ") + cyan(value)
    print(line)


def _summarize(labels: Sequence[str], limit: int = 3) -> str:
    shown = ", ".join(labels[:limit])
    if len(labels) > limit:
        shown += f" (+{len(labels) - limit})"
    return shown


# ---------------------------------------------------------------------------
# selector engine

def _matches(choice: Choice, query: str) -> bool:
    hay = _plain(choice.label + " " + choice.hint).lower()
    return all(tok in hay for tok in query.lower().split())


def _move(visible: Sequence[Choice], pos: int, step: int) -> int:
    if not visible:
        return 0
    idx = pos
    for _ in range(len(visible)):
        idx = (idx + step) % len(visible)
        if visible[idx].enabled:
            return idx
    return pos


def _select(title: str, choices: Sequence[Choice], multi: bool) -> List[Choice]:
    if not any(c.enabled for c in choices):
        raise CLIError("nothing selectable")
    frame = _Frame()
    width = max(40, shutil.get_terminal_size((100, 24)).columns - 1)
    query = ""
    filtering = False
    pos = 0
    selected: set = set()  # indices into `choices`
    index_of = {id(c): i for i, c in enumerate(choices)}

    def visible_choices() -> List[Choice]:
        return [c for c in choices if _matches(c, query)] if query else list(choices)

    visible = visible_choices()
    pos = _move(visible, len(visible) - 1, 1)  # first enabled

    def render() -> None:
        lines = [_header()]
        vis = visible
        if not vis:
            lines.append(f"{dim(GLYPH_BAR)} {dim('(no matches)')}")
        else:
            start = max(0, min(pos - MAX_ROWS // 2, len(vis) - MAX_ROWS))
            end = min(len(vis), start + MAX_ROWS)
            if start > 0:
                lines.append(f"{dim(GLYPH_BAR)} {dim(f'↑ {start} more')}")
            for idx in range(start, end):
                lines.append(_row(vis[idx], idx == pos))
            if end < len(vis):
                lines.append(f"{dim(GLYPH_BAR)} {dim(f'↓ {len(vis) - end} more')}")
        lines.append(f"{dim(GLYPH_END)} {dim(_hints())}")
        frame.draw(lines)

    def _header() -> str:
        head = f"{cyan(GLYPH_ACTIVE)} {bold(title)}"
        extra = []
        if multi and selected:
            extra.append(f"{len(selected)} selected")
        if query:
            extra.append(f"filter: {query}" + ("▌" if filtering else f" ({len(visible)}/{len(choices)})"))
        elif filtering:
            extra.append("filter: ▌")
        if extra:
            head += dim("  · " + " · ".join(extra))
        return head

    def _row(choice: Choice, is_cursor: bool) -> str:
        if multi:
            mark = "◼" if index_of[id(choice)] in selected else "◻"
        else:
            mark = "●" if is_cursor else "○"
        body = _wtrunc(_plain(choice.label), width - 30)
        pad = " " if choice.hint else ""
        hint = _wtrunc(_plain(choice.hint), width - 6 - sum(_cwidth(c) for c in body)) if choice.hint else ""
        if not choice.enabled:
            return f"{dim(GLYPH_BAR)} {dim(mark + ' ' + body + ' (unavailable)')}"
        if is_cursor:
            return f"{cyan(GLYPH_BAR)} {cyan(mark)} {bold(body)}{pad}{dim(hint)}"
        return f"{dim(GLYPH_BAR)} {dim(mark)} {body}{pad}{dim(hint)}"

    def _hints() -> str:
        if filtering:
            return "type to filter · enter done · esc clear"
        parts = ["↑↓ move"]
        if multi:
            parts += ["space select", "a all"]
        parts += ["/ filter", "enter confirm", "q cancel"]
        return " · ".join(parts)

    sys.stdout.write("\x1b[?25l")
    try:
        while True:
            render()
            key = read_key(chars=filtering)
            if filtering:
                if key == "quit":
                    raise KeyboardInterrupt
                if key in {"up", "down", "home", "end"}:
                    filtering = False  # leave filter mode, then navigate below
                else:
                    if key == "enter":
                        filtering = False
                    elif key == "escape":
                        filtering, query = False, ""
                    elif key == "backspace":
                        if query:
                            query = query[:-1]
                        else:
                            filtering = False
                    elif key:
                        query += key
                    visible = visible_choices()
                    pos = min(pos, max(0, len(visible) - 1))
                    if visible and not visible[pos].enabled:
                        pos = _move(visible, len(visible) - 1, 1)
                    continue
            if key == "up":
                pos = _move(visible, pos, -1)
            elif key == "down":
                pos = _move(visible, pos, 1)
            elif key == "home":
                pos = _move(visible, len(visible) - 1, 1)
            elif key == "end":
                pos = _move(visible, 0, -1)
            elif key == "filter":
                filtering = True
            elif key == "backspace" and query:
                filtering = True
                query = query[:-1]
                visible = visible_choices()
                pos = min(pos, max(0, len(visible) - 1))
                if visible and not visible[pos].enabled:
                    pos = _move(visible, len(visible) - 1, 1)
            elif key == "space" and multi and visible and visible[pos].enabled:
                selected.symmetric_difference_update({index_of[id(visible[pos])]})
            elif key == "all" and multi:
                vis_enabled = {index_of[id(c)] for c in visible if c.enabled}
                selected = selected - vis_enabled if vis_enabled <= selected else selected | vis_enabled
            elif key == "enter":
                if not visible:
                    continue
                if multi:
                    if not selected and visible[pos].enabled:
                        selected = {index_of[id(visible[pos])]}
                    if not selected:
                        continue
                    picked = [choices[i] for i in sorted(selected)]
                elif visible[pos].enabled:
                    picked = [visible[pos]]
                else:
                    continue
                frame.clear()
                step_done(title, _summarize([c.label for c in picked]))
                return picked
            elif key in {"quit", "escape"}:
                if query:
                    query = ""
                    visible = visible_choices()
                    pos = _move(visible, len(visible) - 1, 1)
                    continue
                raise KeyboardInterrupt
    except KeyboardInterrupt:
        frame.clear()
        step_done(title, "cancelled")
        sys.stdout.write("\x1b[?25h")
        sys.stdout.flush()
        raise SystemExit(130)
    finally:
        sys.stdout.write("\x1b[?25h")
        sys.stdout.flush()


def select_one(title: str, choices: Sequence[Choice]) -> Choice:
    return _select(title, choices, multi=False)[0]


def select_many(title: str, choices: Sequence[Choice]) -> List[Choice]:
    return _select(title, choices, multi=True)


def prompt_path(prompt: str, must_exist: bool = False) -> Path:
    value = input(f"{cyan(GLYPH_ACTIVE)} {prompt}").strip()
    if not value:
        raise CLIError("empty path")
    path = Path(value).expanduser().resolve()
    if must_exist and not path.exists():
        raise CLIError(f"path not found: {path}")
    return path
