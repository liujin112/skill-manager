# Repo-side skill registry for skill-manager: discover collections (dirs under
# projects/ that contain skills/), parse SKILL.md frontmatter for descriptions,
# and resolve skill references given as "name" or "collection/name" (npm-scope
# style). A legacy layout with collections at the repo root is still supported.
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

IGNORE_NAMES = {".DS_Store", "__pycache__", ".git"}


class CLIError(Exception):
    """User-facing error; printed without a traceback."""


def read_frontmatter(skill_md: Path) -> Dict[str, str]:
    """Minimal YAML frontmatter reader: top-level `key: value` pairs and
    block scalars (`key: |`). Enough for SKILL.md metadata; no external deps."""
    try:
        lines = skill_md.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    if not lines or lines[0].strip() != "---":
        return {}
    meta: Dict[str, str] = {}
    block_key: Optional[str] = None
    block_lines: List[str] = []

    def flush_block() -> None:
        nonlocal block_key, block_lines
        if block_key is not None:
            meta[block_key] = "\n".join(block_lines).strip()
        block_key, block_lines = None, []

    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line[:1] not in (" ", "\t") and ":" in line:
            flush_block()
            key, _, value = line.partition(":")
            key, value = key.strip(), value.strip()
            if value in {"|", ">", "|-", ">-", "|+", ">+"}:
                block_key = key
            else:
                meta[key] = value.strip("'\"")
        elif block_key is not None:
            block_lines.append(line.strip())
    flush_block()
    return meta


def _normalize(name: str) -> str:
    return name.strip().lower().replace("-", "_")


def first_token(name: str) -> str:
    """First word of a collection name ('lark_skills' -> 'lark'), used for
    shorthand flags/aliases like --lark."""
    return re.split(r"[-_]", _normalize(name))[0]


def find_skill_dirs(skills_root: Path) -> List[Path]:
    """Immediate subdirectories that contain a SKILL.md."""
    if not skills_root.is_dir():
        return []
    return [
        child
        for child in sorted(skills_root.iterdir(), key=lambda p: p.name.lower())
        if child.is_dir() and (child / "SKILL.md").is_file()
    ]


def description_of(skill_dir: Path) -> str:
    """First line of the SKILL.md description, for one-line listings."""
    desc = read_frontmatter(skill_dir / "SKILL.md").get("description", "")
    return desc.splitlines()[0].strip() if desc else ""


@dataclass
class Skill:
    name: str
    collection: str
    path: Path
    _meta: Optional[Dict[str, str]] = field(default=None, repr=False)

    @property
    def ref(self) -> str:
        return f"{self.collection}/{self.name}"

    @property
    def meta(self) -> Dict[str, str]:
        if self._meta is None:
            self._meta = read_frontmatter(self.path / "SKILL.md")
        return self._meta

    @property
    def description(self) -> str:
        desc = self.meta.get("description", "")
        return desc.splitlines()[0].strip() if desc else ""


class Repo:
    """The skill-manager repository: collections under projects/, each with a
    skills/ directory (falls back to collections at the repo root)."""

    def __init__(self, root: Path):
        self.root = root

    @property
    def projects_dir(self) -> Path:
        projects = self.root / "projects"
        if projects.is_dir():
            return projects
        try:  # legacy layout: collections sitting directly at the repo root
            if any((c / "skills").is_dir() for c in self.root.iterdir() if c.is_dir()):
                return self.root
        except OSError:
            pass
        return projects  # fresh repo: default to projects/ (created on demand)

    def collections(self) -> List[str]:
        try:
            children = sorted(self.projects_dir.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return []
        return [c.name for c in children if c.is_dir() and (c / "skills").is_dir()]

    def skills_dir(self, collection: str) -> Path:
        return self.projects_dir / collection / "skills"

    def skills(self, collections: Optional[List[str]] = None) -> List[Skill]:
        result: List[Skill] = []
        for coll in collections or self.collections():
            for child in find_skill_dirs(self.skills_dir(coll)):
                result.append(Skill(child.name, coll, child))
        return result

    def match_collection(self, raw: str) -> str:
        """Accept the exact collection name, dash/underscore variants, or a
        unique first-token shorthand (merlin, lark, nature, tcs, traex)."""
        names = self.collections()
        norm = _normalize(raw)
        for name in names:
            if norm == _normalize(name):
                return name
        prefix_hits = [n for n in names if first_token(n) == norm]
        if len(prefix_hits) == 1:
            return prefix_hits[0]
        raise CLIError(
            f"unknown collection '{raw}'; available: " + ", ".join(names)
        )

    def resolve(self, ref: str, collections: Optional[List[str]] = None) -> Skill:
        """Resolve 'name' (searched across collections) or 'collection/name'."""
        ref = ref.strip()
        if "/" in ref:
            coll_raw, _, name = ref.partition("/")
            coll = self.match_collection(coll_raw)
            path = self.skills_dir(coll) / name
            if not (path / "SKILL.md").is_file():
                raise CLIError(f"skill not found: {coll}/{name}")
            return Skill(name, coll, path)
        matches = [s for s in self.skills(collections) if s.name == ref]
        if not matches:
            raise CLIError(f"skill not found: {ref} (try: skill search {ref})")
        if len(matches) > 1:
            refs = ", ".join(s.ref for s in matches)
            raise CLIError(f"'{ref}' exists in multiple collections; use one of: {refs}")
        return matches[0]


def find_repo_root(cli_override: Optional[str] = None) -> Path:
    """Locate the skill-manager repo: explicit flag > env var > this file's repo."""
    candidates: List[Path] = []
    if cli_override:
        candidates.append(Path(cli_override).expanduser())
    for env in ("SKILL_REPO_ROOT", "SKILLCTL_REPO_ROOT"):
        value = os.environ.get(env)
        if value:
            candidates.append(Path(value).expanduser())
    for cand in candidates:
        try:
            cand = cand.resolve()
        except OSError:
            continue
        if Repo(cand).collections():
            return cand
    # fall back to the repo the tool lives in — valid even with no collections
    # yet (a fresh clone): `skill get` creates projects/ on first import
    return Path(__file__).resolve().parents[2]
