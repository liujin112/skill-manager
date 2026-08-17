# File operations on skill directories: copy (install/save), remove
# (uninstall), symlink whole skills dirs (link), and content digests used to
# tell "up to date" from "modified" (npm-style: installing an identical copy
# is a no-op, not an overwrite error).
from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Set

from .registry import IGNORE_NAMES, find_skill_dirs


@dataclass
class OpResult:
    name: str
    action: str  # installed | updated | up-to-date | skipped | removed | would-*
    target: Path
    note: str = ""


def _ignored(name: str) -> bool:
    return name in IGNORE_NAMES or name.endswith(".pyc")


def _ignore_copy(_dir: str, names: Iterable[str]) -> Set[str]:
    return {name for name in names if _ignored(name)}


def skill_digest(skill_dir: Path) -> str:
    """Content hash of a skill directory (ignoring junk files), for comparisons."""
    digest = hashlib.sha256()
    try:
        files = sorted(
            p for p in skill_dir.rglob("*")
            if p.is_file() and not any(_ignored(part) for part in p.relative_to(skill_dir).parts)
        )
    except OSError:
        return ""
    for file in files:
        digest.update(file.relative_to(skill_dir).as_posix().encode())
        digest.update(b"\0")
        try:
            digest.update(file.read_bytes())
        except OSError:
            digest.update(str(file).encode())  # e.g. broken symlink
        digest.update(b"\0")
    return digest.hexdigest()


def copy_skill(source: Path, target_root: Path, force: bool, dry_run: bool,
               verb: str = "install") -> OpResult:
    """`verb` names the operation in results: install(ed) / save(d) / merge(d)."""
    name = source.name
    target = target_root / name
    if target.exists() and source.resolve() == target.resolve():
        return OpResult(name, "skipped", target, "source and target are the same directory")
    if not (source / "SKILL.md").is_file():
        return OpResult(name, "skipped", target, "no SKILL.md in source")
    exists = target.exists()
    if exists and skill_digest(source) == skill_digest(target):
        return OpResult(name, "up-to-date", target)
    if exists and not force:
        return OpResult(name, "skipped", target, "target differs; use --force to overwrite")
    if dry_run:
        return OpResult(name, "would-update" if exists else f"would-{verb}", target)
    action = "updated" if exists else verb + ("d" if verb.endswith("e") else "ed")
    target_root.mkdir(parents=True, exist_ok=True)
    if exists:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    shutil.copytree(source, target, symlinks=True, ignore=_ignore_copy)
    return OpResult(name, action, target)


def link_skills_dir(source: Path, target: Path, force: bool, dry_run: bool) -> List[OpResult]:
    """Turn `target` into a symlink pointing at `source`, so agents share one
    skills directory. Skills that exist only in `target` are copied into
    `source` first, so nothing is lost. Local copies that differ from the
    source version block the link unless --force (save them first instead)."""
    src = source.resolve() if source.exists() else source  # link to the real dir, not a chain
    arrow = f"-> {src}"
    if target.is_symlink():
        if target.resolve() == src:
            return [OpResult(arrow, "up-to-date", target, "already linked")]
        if not force:
            return [OpResult(arrow, "skipped", target,
                             f"already links to {target.resolve()}; use --force to relink")]
    elif target.resolve() == src:
        return [OpResult(arrow, "skipped", target, "target is the source directory itself")]

    results: List[OpResult] = []
    if target.is_dir() and not target.is_symlink():
        skills = find_skill_dirs(target)
        stray = [p.name for p in target.iterdir()
                 if p.name not in IGNORE_NAMES and p not in skills]
        if stray and not force:
            return [OpResult(arrow, "skipped", target,
                             "directory has non-skill entries (" + ", ".join(stray[:5]) + "); use --force")]
        differing = [d.name for d in skills
                     if (src / d.name / "SKILL.md").is_file()
                     and skill_digest(d) != skill_digest(src / d.name)]
        if differing and not force:
            return [OpResult(arrow, "skipped", target,
                             "local copies differ from source: " + ", ".join(differing)
                             + "; `skill save` them first, or --force to drop them")]
        for skill in skills:
            if not (src / skill.name / "SKILL.md").is_file():
                moved = copy_skill(skill, src, force=False, dry_run=dry_run, verb="merge")
                moved.note = "copied into source before linking"
                results.append(moved)
    elif target.exists() and not force:  # a plain file in the way
        return [OpResult(arrow, "skipped", target, "target exists and is not a directory; use --force")]

    if dry_run:
        results.append(OpResult(arrow, "would-link", target))
        return results
    src.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(src, target_is_directory=True)
    results.append(OpResult(arrow, "linked", target))
    return results


def remove_skill(target_root: Path, name: str, dry_run: bool) -> OpResult:
    target = target_root / name
    if not target.exists():
        return OpResult(name, "skipped", target, "not installed here")
    if not target.is_symlink() and not (target / "SKILL.md").is_file():
        return OpResult(name, "skipped", target, "not a skill directory; refusing to delete")
    if dry_run:
        return OpResult(name, "would-remove", target)
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    else:
        target.unlink()
    return OpResult(name, "removed", target)
