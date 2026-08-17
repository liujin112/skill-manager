# Fetching skills from git hosts for `skill get`: parse GitHub-style URLs
# (including /tree/<branch>/<subdir> links), clone or update the repo under
# <skill-manager>/repos/, and discover skill directories (a root SKILL.md
# means the whole repo is one skill; otherwise every dir containing SKILL.md).
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .registry import CLIError

SKIP_DIRS = {".git", ".github", "node_modules", "__pycache__", "venv", ".venv"}


@dataclass(frozen=True)
class GitSource:
    clone_url: str
    name: str  # repo name; also the default collection name and clone dir name
    branch: Optional[str] = None
    subpath: str = ""  # only scan this subdirectory for skills


def is_git_url(raw: str) -> bool:
    """Unambiguous git URLs only — owner/repo shorthand is excluded because it
    collides with the collection/name skill-ref syntax."""
    return raw.startswith(("http://", "https://", "git@", "file://")) or raw.endswith(".git")


def parse_git_url(raw: str) -> GitSource:
    raw = raw.strip().rstrip("/")
    m = re.match(
        r"^(?:https?://github\.com/)([^/]+)/([^/]+?)(?:\.git)?(?:/tree/([^/]+)(?:/(.*))?)?$",
        raw,
    )
    if m:
        owner, name, branch, subpath = m.groups()
        return GitSource(f"https://github.com/{owner}/{name}.git", name, branch, subpath or "")
    if raw.startswith(("git@", "http://", "https://", "file://")) or raw.endswith(".git"):
        name = raw.split("/")[-1]
        if name.endswith(".git"):
            name = name[:-4]
        if not name:
            raise CLIError(f"cannot parse git URL: {raw}")
        return GitSource(raw, name)
    m = re.match(r"^([\w.-]+)/([\w.-]+)$", raw)
    if m:  # npm-style shorthand: owner/repo -> github
        owner, name = m.groups()
        return GitSource(f"https://github.com/{owner}/{name}.git", name)
    raise CLIError(f"cannot parse git URL: {raw}")


def _run_git(git_args: List[str]) -> str:
    proc = subprocess.run(["git", *git_args], capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise CLIError(f"git {' '.join(git_args[:2])} failed: {detail[:400]}")
    return proc.stdout


def _norm_remote(url: str) -> str:
    return url.strip().rstrip("/").removesuffix(".git").lower()


def ensure_clone(source: GitSource, dest: Path) -> str:
    """Clone `source` into `dest`, or fast-forward an existing clone.
    Returns 'cloned' or 'updated'."""
    if (dest / ".git").exists():
        remote = _run_git(["-C", str(dest), "remote", "get-url", "origin"]).strip()
        if _norm_remote(remote) != _norm_remote(source.clone_url):
            raise CLIError(
                f"{dest} already exists but tracks a different remote ({remote}); remove it first"
            )
        _run_git(["-C", str(dest), "pull", "--ff-only"])
        return "updated"
    if dest.exists():
        raise CLIError(f"{dest} exists but is not a git clone; remove it first")
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["clone", "--depth", "1"]
    if source.branch:
        cmd += ["--branch", source.branch]
    cmd += [source.clone_url, str(dest)]
    _run_git(cmd)
    return "cloned"


def head_commit(clone_dir: Path) -> str:
    return _run_git(["-C", str(clone_dir), "rev-parse", "HEAD"]).strip()


def discover_skills(scan_root: Path) -> List[Path]:
    """Directories that hold a SKILL.md. A SKILL.md at scan_root means the
    whole tree is one skill; otherwise search recursively, skipping junk."""
    if not scan_root.is_dir():
        raise CLIError(f"path not found in clone: {scan_root}")
    if (scan_root / "SKILL.md").is_file():
        return [scan_root]
    found: List[Path] = []
    for md in sorted(scan_root.rglob("SKILL.md")):
        dir_parts = md.relative_to(scan_root).parts[:-1]
        if any(part in SKIP_DIRS or part.startswith(".") for part in dir_parts):
            continue
        found.append(md.parent)
    return found
