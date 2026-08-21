# Git plumbing for the skill library repo itself: the pull half of `skill
# update` (fast-forward the checkout) and all of `skill sync` (stage, commit,
# pull --rebase, push). Thin wrappers over the git CLI — nothing here touches
# skill contents. Network commands never prompt (GIT_TERMINAL_PROMPT=0) and
# time out instead of hanging the CLI.
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .registry import CLIError

NET_TIMEOUT = 180  # seconds for fetch/pull/push


def try_run(root: Path, *args: str, timeout: Optional[int] = None) -> Tuple[int, str, str]:
    """Run git in `root`; returns (returncode, stdout, stderr) — never raises.
    Output is returned verbatim: `status --porcelain` encodes the staged/
    unstaged distinction in leading spaces, so stripping it here would corrupt
    the parse."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                              text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", f"git {args[0]} timed out after {timeout}s"
    except OSError as exc:
        return 127, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def _token(root: Path, *args: str) -> str:
    """A single-value git query ('' when the command fails)."""
    code, out, _ = try_run(root, *args)
    return out.strip() if code == 0 else ""


def _fail(args: Sequence[str], out: str, err: str) -> str:
    return (err.strip() or out.strip() or f"git {args[0]} failed")


def run(root: Path, *args: str, timeout: Optional[int] = None) -> str:
    code, out, err = try_run(root, *args, timeout=timeout)
    if code != 0:
        raise CLIError(f"git {args[0]} failed in {root}: {_fail(args, out, err)[:400]}")
    return out.strip()


def is_repo(root: Path) -> bool:
    return try_run(root, "rev-parse", "--is-inside-work-tree")[0] == 0


@dataclass
class State:
    branch: str
    head: str
    upstream: Optional[str] = None
    ahead: int = 0
    behind: int = 0
    changes: List[Tuple[str, str]] = field(default_factory=list)  # (status, path)

    @property
    def dirty(self) -> bool:
        return bool(self.changes)

    @property
    def short(self) -> str:
        return self.head[:7] if self.head else "-"


def _porcelain(root: Path) -> List[Tuple[str, str]]:
    """Pending changes as (status, path): A added/untracked, M modified,
    D deleted, R renamed — the index column when staged, else the worktree one."""
    out = try_run(root, "status", "--porcelain", "-uall")[1]
    entries: List[Tuple[str, str]] = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        x, y, path = line[0], line[1], line[3:]
        if " -> " in path:  # rename: report the destination
            path = path.split(" -> ", 1)[1]
        status = "A" if x == "?" else (x if x != " " else y)
        entries.append((status, path.strip().strip('"')))
    return entries


def state(root: Path) -> State:
    st = State(
        branch=_token(root, "rev-parse", "--abbrev-ref", "HEAD") or "HEAD",
        head=_token(root, "rev-parse", "HEAD"),
        upstream=_token(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}") or None,
        changes=_porcelain(root),
    )
    if st.upstream:
        parts = _token(root, "rev-list", "--left-right", "--count", f"{st.upstream}...HEAD").split()
        if len(parts) == 2:
            st.behind, st.ahead = int(parts[0]), int(parts[1])
    return st


def default_remote(root: Path) -> Optional[str]:
    remotes = try_run(root, "remote")[1].split()
    if not remotes:
        return None
    return "origin" if "origin" in remotes else remotes[0]


def fetch(root: Path) -> Optional[str]:
    """Fetch the default remote. Returns an error message, or None on success."""
    remote = default_remote(root)
    if not remote:
        return "no git remote configured"
    code, out, err = try_run(root, "fetch", "--quiet", remote, timeout=NET_TIMEOUT)
    return None if code == 0 else _fail(["fetch"], out, err)


def pull_ff(root: Path) -> Optional[str]:
    """Fast-forward the current branch to its upstream (no merge commits)."""
    code, out, err = try_run(root, "merge", "--ff-only", "@{u}", timeout=NET_TIMEOUT)
    return None if code == 0 else _fail(["merge"], out, err)


def pull_rebase(root: Path) -> Optional[str]:
    """Rebase local commits onto the upstream. A conflicting rebase is aborted
    so the checkout is left exactly as it was, and reported for manual fixing."""
    code, out, err = try_run(root, "pull", "--rebase", timeout=NET_TIMEOUT)
    if code == 0:
        return None
    git_dir = Path(_token(root, "rev-parse", "--git-dir") or ".git")
    if not git_dir.is_absolute():
        git_dir = root / git_dir
    if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
        try_run(root, "rebase", "--abort")
        return _fail(["pull"], out, err) + " (rebase aborted; nothing was changed)"
    return _fail(["pull"], out, err)


def stage_all(root: Path) -> None:
    run(root, "add", "-A")


def commit(root: Path, message: str) -> None:
    code, out, err = try_run(root, "commit", "-m", message)
    if code != 0:
        raise CLIError(f"git commit failed: {_fail(['commit'], out, err)[:400]}")


def push(root: Path, branch: str, set_upstream: bool) -> Optional[str]:
    args = ["push"]
    if set_upstream:
        args += ["-u", default_remote(root) or "origin", branch]
    code, out, err = try_run(root, *args, timeout=NET_TIMEOUT)
    return None if code == 0 else _fail(["push"], out, err)


def disk_version(tool_root: Path) -> str:
    """__version__ as it is on disk — after a pull this differs from the
    version of the code currently running."""
    try:
        text = (tool_root / "lib" / "skillcli" / "__init__.py").read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else ""
