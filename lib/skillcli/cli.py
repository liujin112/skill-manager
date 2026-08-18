# Command definitions for the `skill` CLI (npm-style manager for the
# skill-manager repo). Commands: list / search / info / install / uninstall /
# save / link / status / doctor / setup. Collections are addressed with
# -r/--collection (shorthand names like 'lark' match lark_skills) or the
# collection/name skill-ref syntax — never baked into --flags.
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
import textwrap
from datetime import datetime, timezone
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import __version__
from .colors import bold, cyan, dim, green, red, yellow
from .github import GitSource, discover_skills, ensure_clone, head_commit, is_git_url, parse_git_url
from .ops import OpResult, copy_skill, link_skills_dir, remove_skill, skill_digest
from .registry import (
    CLIError,
    Repo,
    Skill,
    description_of,
    find_repo_root,
    find_skill_dirs,
)
from .targets import (
    AGENTS,
    Target,
    is_bare_agent,
    known_targets,
    parse_target_list,
    resolve_target,
    split_specs,
)
from . import tui
from .tui import Choice

PROG = "skill"


# ---------------------------------------------------------------------------
# small shared helpers

def _repo(args: argparse.Namespace) -> Repo:
    return Repo(find_repo_root(getattr(args, "repo_root", None)))


def _project_root(args: argparse.Namespace) -> Path:
    raw = getattr(args, "project_root", None)
    return Path(raw).expanduser().resolve() if raw else Path.cwd().resolve()


def _interactive(args: argparse.Namespace) -> bool:
    return tui.is_tty() and not getattr(args, "non_interactive", False)


def _requested_refs(args: argparse.Namespace) -> List[str]:
    """Positional skill names plus the legacy -s/--skills comma list."""
    refs: List[str] = []
    for value in getattr(args, "refs", None) or []:
        refs.extend(x.strip() for x in value.split(",") if x.strip())
    if getattr(args, "skills_opt", None):
        refs.extend(x.strip() for x in args.skills_opt.split(",") if x.strip())
    return refs


def _selected_collections(repo: Repo, args: argparse.Namespace) -> Optional[List[str]]:
    raw = [item for value in (getattr(args, "collection_opt", None) or [])
           for item in value.split(",") if item.strip()]
    if not raw:
        return None
    seen: List[str] = []
    for item in raw:
        name = repo.match_collection(item)
        if name not in seen:
            seen.append(name)
    return seen


def _term_width() -> int:
    return shutil.get_terminal_size((100, 24)).columns


def _trunc(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


def _print_skill_rows(pairs: Sequence[Tuple[str, str]]) -> None:
    if not pairs:
        print(dim("  (empty)"))
        return
    name_w = min(max(len(name) for name, _ in pairs), 32)
    desc_w = max(20, _term_width() - name_w - 6)
    for name, desc in pairs:
        print(f"  {cyan(name.ljust(name_w))}  {dim(_trunc(desc, desc_w))}")


def _target_header(target: Target) -> str:
    if target.scope == "custom":
        return str(target.path)
    return f"{target.label}  {dim(str(target.path))}"


def _print_results(groups: Sequence[Tuple[str, Sequence[OpResult]]]) -> None:
    counts: Counter = Counter()
    for label, results in groups:
        print("\n" + bold(label))
        for result in results:
            counts[result.action] += 1
            if result.action.startswith("would-"):
                style = cyan
            elif result.action in {"installed", "updated", "removed", "saved", "merged", "linked", "imported"}:
                style = green
            elif result.action == "up-to-date":
                style = dim
            else:
                style = yellow
            line = f"  {style(result.action.ljust(12))} {result.name}"
            if result.note:
                line += dim(f"  ({result.note})")
            print(line)
    print("\n" + ", ".join(f"{count} {action}" for action, count in sorted(counts.items())))


def _confirm_plan(action: str, names: Sequence[str], where: str, args: argparse.Namespace,
                  noun: str = "skill(s)") -> None:
    print(f"\n{bold(action)} {len(names)} {noun} -> {cyan(where)}")
    print(textwrap.fill(", ".join(names), max(40, _term_width() - 4),
                        initial_indent="  ", subsequent_indent="  "))
    if getattr(args, "yes", False) or not _interactive(args):
        return
    answer = input(f"{cyan(tui.GLYPH_ACTIVE)} Proceed? [Y/n] ").strip().lower()
    if answer in {"n", "no", "q", "quit"}:
        raise SystemExit(130)


def _confirm_overwrite(skills: Sequence[Skill], targets: Sequence[Target], args: argparse.Namespace) -> bool:
    """Ask about targets that exist with *different* content (identical copies
    are reported as up-to-date and never need --force)."""
    if args.force:
        return True
    diffs: List[str] = []
    for target in targets:
        for skill in skills:
            dest = target.path / skill.name
            if dest.exists() and skill_digest(dest) != skill_digest(skill.path):
                diffs.append(f"{skill.name} -> {target.label}")
    if not diffs:
        return False
    print("\nAlready present with different content:")
    for item in diffs:
        print(f"  {yellow(item)}")
    if _interactive(args):
        answer = input(f"{cyan(tui.GLYPH_ACTIVE)} Overwrite these? [y/N] ").strip().lower()
        return answer in {"y", "yes"}
    print("They will be skipped. Use --force to overwrite.")
    return False


# ---------------------------------------------------------------------------
# interactive pickers

def _pick_collections(repo: Repo, title: str, allow_all: bool, allow_new: bool = False):
    """Returns None (= all), a [collection] list, or '__new__' when allow_new."""
    choices = []
    if allow_all:
        choices.append(Choice("all collections", None))
    for coll in repo.collections():
        count = len(repo.skills([coll]))
        choices.append(Choice(coll, [coll], True, dim(f"({count} skills)")))
    if allow_new:
        choices.append(Choice("new collection...", "__new__"))
    picked = tui.select_one(title, choices)
    return picked.value


def _create_collection(repo: Repo, name: str, dry_run: bool = False) -> str:
    name = name.strip()
    if not name or "/" in name or name.startswith("."):
        raise CLIError(f"invalid collection name: '{name}'")
    skills_dir = repo.skills_dir(name)
    if dry_run:
        print(cyan(f"would create collection: {name}") + dim(f"  ({skills_dir})"))
        return name
    skills_dir.mkdir(parents=True, exist_ok=True)
    print(green(f"created collection: {name}") + dim(f"  ({skills_dir})"))
    return name


def _resolve_or_create_collection(repo: Repo, raw: str, args: argparse.Namespace) -> str:
    """Match an existing collection; unknown names offer to create a new one
    (auto-created with -y in script mode) instead of erroring outright."""
    try:
        return repo.match_collection(raw)
    except CLIError:
        pass
    if _interactive(args) and not getattr(args, "yes", False):
        answer = input(f"{cyan(tui.GLYPH_ACTIVE)} Collection '{raw}' does not exist. Create it? [Y/n] ").strip().lower()
        if answer in {"n", "no", "q", "quit"}:
            raise SystemExit(130)
    elif not getattr(args, "yes", False):
        raise CLIError(
            f"collection '{raw}' does not exist (available: "
            + ", ".join(repo.collections()) + "); pass -y to create it"
        )
    return _create_collection(repo, raw, getattr(args, "dry_run", False))


def _pick_repo_skills(repo: Repo, colls: Optional[List[str]], title: str) -> List[Skill]:
    skills = repo.skills(colls)
    if not skills:
        raise CLIError("no skills found in " + ", ".join(colls or repo.collections()))
    show_ref = colls is None or len(colls) > 1
    choices = [
        Choice(s.ref if show_ref else s.name, s, True, dim(_trunc(s.description, 60)))
        for s in skills
    ]
    return [c.value for c in tui.select_many(title, choices)]  # type: ignore[list-item]


def _ask_scope(project_root: Path) -> str:
    """One g/l keystroke choosing global (~/.<agent>/skills) vs project-local
    scope; the prompt collapses to a one-line summary once answered."""
    plain = f"Scope · [g]lobal ~/.<agent>/skills · [l]ocal {project_root}/.<agent>/skills [G/l] "
    sys.stdout.write(cyan(tui.GLYPH_ACTIVE) + " " + _trunc(plain, _term_width() - 3))
    sys.stdout.flush()
    while True:
        key = tui.read_key()
        if key in {"g", "G", "enter", "space"}:
            answer = "global"
        elif key in {"l", "L"}:
            answer = "local"
        elif key in {"quit", "escape"}:
            sys.stdout.write("\r\x1b[2K")
            tui.step_done("Scope", "cancelled")
            raise SystemExit(130)
        else:
            continue
        sys.stdout.write("\r\x1b[2K")
        tui.step_done("Scope", answer)
        return answer


def _targets_from_specs(values: List[str], project_root: Path, args: argparse.Namespace) -> List[Target]:
    """Resolve user-given target specs. Explicit scope ('claude:local', 'tg',
    a path) is taken as-is; bare agent names ('claude') use the -g/-l flag if
    given, otherwise ask global/local once on a TTY, and default to global
    with -N/-y (script mode)."""
    specs = split_specs(values)
    scope = getattr(args, "scope", None)
    if scope is None:
        scope = "global"
        if any(is_bare_agent(s) for s in specs) and _interactive(args) and not getattr(args, "yes", False):
            scope = _ask_scope(project_root)
    targets: List[Target] = []
    for spec in specs:
        target = resolve_target(f"{spec}:{scope}" if is_bare_agent(spec) else spec, project_root)
        if target not in targets:
            targets.append(target)
    return targets


def _pick_targets(project_root: Path, title: str, scope: Optional[str] = None) -> List[Target]:
    """Multi-select agents (Space to tick several), then ask global/local once.
    A scope from -g/-l (or --to <agent>:<scope>) skips the question."""
    choices = []
    for agent in AGENTS:
        global_t = resolve_target(agent, project_root)
        local_t = resolve_target(f"{agent}:local", project_root)
        hint = str(global_t.path)
        if global_t.path.exists():
            hint += f" ({len(find_skill_dirs(global_t.path))} skills)"
        if local_t.path.exists():
            hint += f" | local: {len(find_skill_dirs(local_t.path))} skills"
        choices.append(Choice(agent, agent, True, dim(hint)))
    choices.append(Choice("custom path...", "__custom__"))
    picked = tui.select_many(title, choices)
    agents = [c.value for c in picked if c.value != "__custom__"]
    targets: List[Target] = []
    if agents:
        scope = scope or _ask_scope(project_root)
        targets = [resolve_target(f"{agent}:{scope}", project_root) for agent in agents]
    if any(c.value == "__custom__" for c in picked):
        targets.append(Target("path", "custom", tui.prompt_path("Skills directory path: ")))
    if not targets:
        raise CLIError("no target selected")
    return targets


def _pick_link_source(project_root: Path) -> Target:
    """Pick the shared source directory for `skill link` (existing dirs first)."""
    choices = []
    for target in known_targets(project_root):
        if target.scope == "local" and not target.path.exists():
            continue  # keep the list short: locals only when they already exist
        count = len(find_skill_dirs(target.path))
        state = f"{count} skills" if target.path.exists() else "will create"
        choices.append(Choice(target.label, target, True, dim(f"{target.path} ({state})")))
    choices.append(Choice("custom path...", "__custom__"))
    picked = tui.select_one("Which directory is the shared source?", choices)
    if picked.value == "__custom__":
        return Target("path", "custom", tui.prompt_path("Source skills directory: "))
    return picked.value  # type: ignore[return-value]


def _pick_source(project_root: Path, title: str, scope: Optional[str] = None) -> Target:
    """Single-select among agent directories that actually contain skills;
    a -g/-l scope narrows the list to that scope."""
    choices = []
    for target in known_targets(project_root):
        if scope and target.scope != scope:
            continue
        count = len(find_skill_dirs(target.path))
        if count:
            choices.append(Choice(target.label, target, True, dim(f"{target.path} ({count} skills)")))
    choices.append(Choice("custom path...", "__custom__"))
    picked = tui.select_one(title, choices)
    if picked.value == "__custom__":
        return Target("path", "custom", tui.prompt_path("Skills directory path: ", must_exist=True))
    return picked.value  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# commands

def cmd_list(args: argparse.Namespace) -> None:
    what = (args.what or "").lower()
    if what in {"local", "l", "installed"}:
        shown = False
        for target in known_targets(_project_root(args)):
            if target.path.is_symlink():
                shown = True
                count = len(find_skill_dirs(target.path))
                print(bold(target.label) + dim(f"  {target.path} (linked -> {target.path.resolve()}, {count} skills)"))
                print()
                continue
            dirs = find_skill_dirs(target.path)
            if not dirs and not args.all:
                continue
            shown = True
            print(bold(target.label) + dim(f"  {target.path}"))
            _print_skill_rows([(d.name, description_of(d)) for d in dirs])
            print()
        if not shown:
            print("No installed skills found. Try: skill install")
        return

    repo = _repo(args)
    colls = _selected_collections(repo, args)
    if what and what not in {"repo", "r"}:
        colls = (colls or []) + [repo.match_collection(what)]
    total = 0
    for coll in colls or repo.collections():
        skills = repo.skills([coll])
        total += len(skills)
        header = bold(coll) + dim(f"  ({len(skills)} skills)")
        manifest = repo.skills_dir(coll).parent / ".source.json"
        if manifest.is_file():
            try:
                meta = json.loads(manifest.read_text(encoding="utf-8"))
                header += dim(f"  [{meta.get('clone_url', '?')} @ {str(meta.get('commit', ''))[:7]}]")
            except (OSError, ValueError):
                pass
        print(header)
        _print_skill_rows([(s.name, s.description) for s in skills])
        print()
    print(dim(f"{total} skills. Install with: skill install <name> --to claude"))


def cmd_search(args: argparse.Namespace) -> None:
    repo = _repo(args)
    kws = [k.lower() for k in args.keywords]
    matches = [
        s for s in repo.skills(_selected_collections(repo, args))
        if all(k in (s.ref + " " + s.description).lower() for k in kws)
    ]
    if not matches:
        print(f"No skills matching '{' '.join(args.keywords)}'.")
        raise SystemExit(1)
    _print_skill_rows([(s.ref, s.description) for s in matches])


def cmd_info(args: argparse.Namespace) -> None:
    repo = _repo(args)
    project_root = _project_root(args)
    for ref in args.refs:
        if "/" in ref:
            matches = [repo.resolve(ref)]
        else:
            matches = [s for s in repo.skills() if s.name == ref]
            if not matches:
                raise CLIError(f"skill not found: {ref} (try: skill search {ref})")
        for skill in matches:
            print(bold(skill.ref))
            desc = skill.meta.get("description", "").strip()
            if desc:
                print(textwrap.indent(desc, "  "))
            for key, value in skill.meta.items():
                if key != "description" and value:
                    print(f"  {dim(key + ':')} {value}")
            files = [p for p in skill.path.rglob("*") if p.is_file()]
            size_kb = sum(p.stat().st_size for p in files) / 1024
            print(f"  {dim('path:')} {skill.path}")
            print(f"  {dim('files:')} {len(files)} ({size_kb:.1f} KB)")
            installed = []
            for target in known_targets(project_root):
                dest = target.path / skill.name
                if (dest / "SKILL.md").is_file():
                    state = "ok" if skill_digest(dest) == skill_digest(skill.path) else "modified"
                    style = green if state == "ok" else yellow
                    installed.append(f"    {target.label.ljust(16)} {style(state)}")
            if installed:
                print(f"  {dim('installed at:')}")
                print("\n".join(installed))
            print()


def cmd_install(args: argparse.Namespace) -> None:
    repo = _repo(args)
    project_root = _project_root(args)
    interactive = _interactive(args)
    colls = _selected_collections(repo, args)
    refs = _requested_refs(args)
    urls = [r for r in refs if is_git_url(r)]
    want_all = args.all or any(r.lower() == "all" for r in refs)

    if urls:
        # npm-style: `skill install <git-url> [names...] --to claude` runs the
        # whole get flow (clone -> import as a collection) and then installs
        if len(urls) > 1:
            raise CLIError("install one git URL at a time")
        get_args = argparse.Namespace(**vars(args))
        get_args.url = urls[0]
        get_args.refs = [r for r in refs if r != urls[0]]
        get_args.skills_opt = None
        get_args.into = (getattr(args, "collection_opt", None) or [None])[0]
        get_args.yes = True  # single confirmation below, at the install step
        collection, chosen = _get_from_git(get_args, repo, interactive)
        selected = [Skill(d.name, collection, d) for d in chosen]
    elif want_all:
        selected = repo.skills(colls)
        if not selected:
            raise CLIError("no skills found in " + ", ".join(colls or repo.collections()))
    elif refs:
        selected = [repo.resolve(ref, colls) for ref in refs]
    elif interactive:
        if colls is None:
            colls = _pick_collections(repo, "Install from which collection?", allow_all=True)
        selected = _pick_repo_skills(repo, colls, "Select skill(s) to install")
    else:
        raise CLIError(
            "nothing selected; pass skill names (skill install merlin-job), "
            "or a collection with --all (skill install -r merlin -a)"
        )
    unique: Dict[str, Skill] = {}
    for skill in selected:
        unique.setdefault(skill.ref, skill)
    selected = list(unique.values())

    if args.to:
        targets = _targets_from_specs(args.to, project_root, args)
    elif os.environ.get("SKILL_DEFAULT_TARGET"):
        targets = parse_target_list([os.environ["SKILL_DEFAULT_TARGET"]], project_root)
    elif interactive:
        targets = _pick_targets(project_root, "Install into which agent(s)?", getattr(args, "scope", None))
    else:
        raise CLIError("--to is required, e.g. --to claude / --to trae:local / --to claude,codex "
                       "(or set SKILL_DEFAULT_TARGET)")

    force = _confirm_overwrite(selected, targets, args)
    _confirm_plan("Install", [s.ref for s in selected], ", ".join(t.label for t in targets), args)
    _print_results([
        (_target_header(t), [copy_skill(s.path, t.path, force, args.dry_run) for s in selected])
        for t in targets
    ])


def cmd_uninstall(args: argparse.Namespace) -> None:
    project_root = _project_root(args)
    interactive = _interactive(args)
    names = [ref.split("/")[-1] for ref in _requested_refs(args)]

    if args.source:
        sources = _targets_from_specs(args.source, project_root, args)
    elif interactive:
        sources = [_pick_source(project_root, "Uninstall from which agent directory?", getattr(args, "scope", None))]
    else:
        raise CLIError("--from is required, e.g. --from claude")

    if not names:
        if not interactive:
            raise CLIError("pass skill names to uninstall")
        choices = [
            Choice(d.name, d.name, True, dim(_trunc(description_of(d), 60)))
            for source in sources for d in find_skill_dirs(source.path)
        ]
        names = sorted({c.value for c in tui.select_many("Select skill(s) to uninstall", choices)})

    where = ", ".join(t.label for t in sources)
    if not args.yes:
        if not interactive:
            raise CLIError("uninstall is destructive; pass --yes in non-interactive mode")
        print(f"\nUninstall {len(names)} skill(s) from {where}:")
        for name in names:
            print(f"  - {name}")
        answer = input("Proceed? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            raise SystemExit(130)
    _print_results([
        (_target_header(t), [remove_skill(t.path, name, args.dry_run) for name in names])
        for t in sources
    ])


def cmd_save(args: argparse.Namespace) -> None:
    repo = _repo(args)
    project_root = _project_root(args)
    interactive = _interactive(args)

    preselected: Optional[str] = None
    if args.source:
        source = _targets_from_specs([args.source[0]], project_root, args)[0]
        # allow --from to point directly at one skill directory
        if source.scope == "custom" and (source.path / "SKILL.md").is_file():
            preselected = source.path.name
            source = Target("path", "custom", source.path.parent)
    elif interactive:
        source = _pick_source(project_root, "Save from which agent directory?", getattr(args, "scope", None))
    else:
        raise CLIError("--from is required, e.g. --from claude")

    available = find_skill_dirs(source.path)
    if not available:
        raise CLIError(f"no skills found in {source.path}")

    # destination collection first, so a bad/new -r fails or gets created
    # before the user invests time picking skills
    if args.into:
        collection = _resolve_or_create_collection(repo, args.into, args)
    elif len(repo.collections()) == 1:
        collection = repo.collections()[0]
    elif interactive:
        picked = _pick_collections(repo, "Save into which collection?", allow_all=False, allow_new=True)
        if picked == "__new__":
            collection = _create_collection(repo, input(f"{cyan(tui.GLYPH_ACTIVE)} New collection name: "))
        else:
            collection = picked[0]  # type: ignore[index]
    else:
        raise CLIError("-r/--into is required, e.g. -r merlin")

    refs = _requested_refs(args) or ([preselected] if preselected else [])
    if args.all or any(r.lower() == "all" for r in refs):
        chosen = available
    elif refs:
        by_name = {d.name: d for d in available}
        missing = [r for r in refs if r not in by_name]
        if missing:
            raise CLIError("skill not found in source: " + ", ".join(missing))
        chosen = [by_name[r] for r in refs]
    elif interactive:
        choices = [Choice(d.name, d, True, dim(_trunc(description_of(d), 60))) for d in available]
        chosen = [c.value for c in tui.select_many(f"Select skill(s) to save into '{collection}'", choices)]
    else:
        raise CLIError("pass skill names or --all")

    dest = repo.skills_dir(collection)
    pseudo = [Skill(d.name, collection, d) for d in chosen]
    force = _confirm_overwrite(pseudo, [Target("repo", "custom", dest)], args)
    _confirm_plan("Save", [d.name for d in chosen], f"{collection} ({dest})", args)
    _print_results([
        (f"{collection}  {dim(str(dest))}",
         [copy_skill(d, dest, force, args.dry_run, verb="save") for d in chosen])
    ])


def _get_from_git(args: argparse.Namespace, repo: Repo, interactive: bool) -> Tuple[str, List[Path]]:
    """Shared clone->discover->select->import flow for `skill get` and
    `skill install <git-url>`. Returns (collection, chosen skill dirs)."""
    source = parse_git_url(args.url)
    clone_dir = repo.root / "repos" / source.name
    state = ensure_clone(source, clone_dir)
    print(f"{state}: {source.clone_url}" + (f" (branch {source.branch})" if source.branch else "")
          + dim(f"  -> {clone_dir}"))

    scan_root = clone_dir / source.subpath if source.subpath else clone_dir
    found = discover_skills(scan_root)
    if not found:
        raise CLIError(f"no SKILL.md found under {scan_root}")
    print(f"\n{len(found)} skill(s) in this repo:")
    _print_skill_rows([(d.name, description_of(d)) for d in found])

    refs = _requested_refs(args)
    if args.all or any(r.lower() == "all" for r in refs):
        chosen = found
    elif refs:
        by_name = {d.name: d for d in found}
        missing = [r for r in refs if r not in by_name]
        if missing:
            raise CLIError("skill not found in repo: " + ", ".join(missing))
        chosen = [by_name[r] for r in refs]
    elif interactive:
        choices = [Choice(d.name, d, True, dim(_trunc(description_of(d), 60))) for d in found]
        chosen = [c.value for c in tui.select_many("Select skill(s) to import", choices)]
    elif len(found) == 1:
        chosen = found
    else:
        raise CLIError("pass skill names or --all")

    collection = _resolve_or_create_collection(repo, args.into or source.name, args)
    dest = repo.skills_dir(collection)
    pseudo = [Skill(d.name, collection, d) for d in chosen]
    force = _confirm_overwrite(pseudo, [Target("repo", "custom", dest)], args)
    _confirm_plan("Import", [d.name for d in chosen], f"{collection} ({dest})", args)
    _print_results([
        (f"{collection}  {dim(str(dest))}",
         [copy_skill(d, dest, force, args.dry_run, verb="import") for d in chosen])
    ])
    if not args.dry_run:
        manifest = _write_source_manifest(repo, collection, source, clone_dir, [d.name for d in chosen])
        print(dim(f"source recorded: {manifest['_path']} (commit {manifest['commit'][:7]})"))
    return collection, chosen


def _write_source_manifest(repo: Repo, collection: str, source: GitSource,
                           clone_dir: Path, names: List[str]) -> dict:
    """Record provenance next to the imported collection, so the upstream URL
    and exact commit survive in this repo even if the author deletes theirs."""
    path = repo.skills_dir(collection).parent / ".source.json"
    previous: dict = {}
    if path.is_file():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = {}
    data = {
        "clone_url": source.clone_url,
        "branch": source.branch,
        "subpath": source.subpath,
        "commit": head_commit(clone_dir),
        "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "skills": sorted(set(previous.get("skills", [])) | set(names)),
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {**data, "_path": str(path)}


def cmd_get(args: argparse.Namespace) -> None:
    repo = _repo(args)
    project_root = _project_root(args)
    interactive = _interactive(args)
    collection, chosen = _get_from_git(args, repo, interactive)
    if args.to:
        targets = _targets_from_specs(args.to, project_root, args)
        _print_results([
            (_target_header(t), [copy_skill(d, t.path, args.force, args.dry_run) for d in chosen])
            for t in targets
        ])
    else:
        print(dim(f"Install with: skill install -r {collection} --to claude"
                  f"   (re-run `skill get {args.url}` later to update)"))


def cmd_link(args: argparse.Namespace) -> None:
    project_root = _project_root(args)
    interactive = _interactive(args)
    specs = split_specs(args.refs)

    if specs:
        # one g/l ask covers every bare spec in the command (source + targets)
        resolved = _targets_from_specs(specs, project_root, args)
        source = resolved[0]
    elif interactive:
        source = _pick_link_source(project_root)
    else:
        raise CLIError("usage: skill link <source> <target...>, e.g. skill link agents claude,trae")

    if len(specs) > 1:
        targets = resolved[1:]
    elif interactive:
        targets = _pick_targets(project_root, f"Link which agent(s) to {source.label}?", getattr(args, "scope", None))
    else:
        raise CLIError("pass at least one target agent, e.g. skill link agents claude,trae")
    targets = [t for t in targets if t.path.expanduser() != source.path.expanduser()]
    if not targets:
        raise CLIError("no targets left (the source cannot link to itself)")

    _confirm_plan("Link", [f"{t.label} -> {source.label}" for t in targets], source.label, args,
                  noun="agent dir(s)")
    groups = [
        (_target_header(t), link_skills_dir(source.path, t.path, args.force, args.dry_run))
        for t in targets
    ]
    _print_results(groups)
    if any(r.action == "linked" for _, results in groups for r in results):
        print(dim(f"Install once into {source.label}; every linked agent sees it immediately."))
        print(dim("To undo a link: just delete the symlink (the shared source keeps everything)."))


def cmd_status(args: argparse.Namespace) -> None:
    repo = _repo(args)
    project_root = _project_root(args)
    index: Dict[str, List[Skill]] = defaultdict(list)
    for skill in repo.skills():
        index[skill.name].append(skill)

    targets = parse_target_list(args.to, project_root) if args.to else known_targets(project_root)
    shown = modified = untracked = 0
    for target in targets:
        if target.path.is_symlink():
            shown += 1
            print(bold(_target_header(target)))
            print(f"  {green('linked'.ljust(10))} {dim('-> ' + str(target.path.resolve()))}")
            print()
            continue
        dirs = find_skill_dirs(target.path)
        if not dirs:
            continue
        shown += 1
        print(bold(_target_header(target)))
        for skill_dir in dirs:
            matches = index.get(skill_dir.name, [])
            if not matches:
                untracked += 1
                state, note = yellow("untracked".ljust(10)), "not in the repo"
            else:
                digest = skill_digest(skill_dir)
                same = [s for s in matches if skill_digest(s.path) == digest]
                if same:
                    state, note = green("ok".ljust(10)), f"= {same[0].ref}"
                else:
                    modified += 1
                    state, note = yellow("modified".ljust(10)), "differs from " + ", ".join(s.ref for s in matches)
            print(f"  {state} {skill_dir.name.ljust(32)} {dim(note)}")
        print()
    if not shown:
        print("No installed skills found. Try: skill install")
        return
    if modified:
        print(dim("modified:  keep local edits with `skill save <name> --from <agent> --into <collection>`,"))
        print(dim("           or restore the repo version with `skill install <name> --to <agent> --force`"))
    if untracked:
        print(dim("untracked: add to the repo with `skill save <name> --from <agent> --into <collection>`"))


def cmd_doctor(args: argparse.Namespace) -> None:
    project_root = _project_root(args)
    print(f"{PROG} {__version__}")
    print(f"entry: {Path(__file__).resolve().parents[2] / 'bin' / 'skill'}")
    try:
        repo = _repo(args)
        print(f"repo:  {repo.root}")
        print("\nCollections:")
        for coll in repo.collections():
            print(f"  {coll.ljust(20)} {len(repo.skills([coll]))} skills")
    except CLIError as exc:
        print(red(f"repo:  {exc}"))
    print(f"\nAgent skill directories (project root: {project_root}):")
    for target in known_targets(project_root):
        count = len(find_skill_dirs(target.path))
        if target.path.is_symlink():
            state = f"linked -> {target.path.resolve()} ({count} skills)"
        else:
            state = f"{count} skills" if target.path.exists() else "missing"
        print(f"  {target.label.ljust(16)} {str(target.path).ljust(60)} {dim(state)}")
    launcher = shutil.which(PROG)
    if not launcher:
        print(f"\nlauncher on PATH: {yellow('not found — run: skill setup')}")
    else:
        try:
            ours = "/bin/skill" in Path(launcher).read_text(errors="ignore")
        except OSError:
            ours = False
        if ours:
            print(f"\nlauncher on PATH: {launcher}")
        else:
            # e.g. /usr/bin/skill from procps shadows us when ~/.local/bin is not first
            print(f"\nlauncher on PATH: {yellow(launcher + ' (not this tool — run: skill setup, and put ~/.local/bin before /usr/bin in PATH)')}")


def cmd_setup(args: argparse.Namespace) -> None:
    entry = Path(__file__).resolve().parents[2] / "bin" / "skill"
    bin_dir = Path(args.bin_dir).expanduser().resolve()
    bin_dir.mkdir(parents=True, exist_ok=True)
    content = f'#!/usr/bin/env bash\nexec {shlex.quote(str(entry))} "$@"\n'
    for name in [args.name, "skillctl"]:  # skillctl kept as a compatibility alias
        launcher = bin_dir / name
        if launcher.exists():
            old = launcher.read_text(errors="ignore") if launcher.is_file() else ""
            ours = "exec" in old and ("/bin/skill" in old or "/bin/skillctl" in old)
            if old != content and not ours and not args.force:
                print(yellow(f"skip {launcher}: exists and was not created by {PROG}; use --force"))
                continue
        launcher.write_text(content)
        launcher.chmod(0o755)
        print(f"installed {launcher} -> {entry}")
    path_entries = [Path(p).expanduser() for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    if bin_dir not in path_entries:
        print(f'\nAdd to your shell config:\n  export PATH="{bin_dir}:$PATH"')
        print(f'  (fish: fish_add_path {bin_dir})')
    print(f"\nTry: {args.name} list")


# ---------------------------------------------------------------------------
# parser

def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-R", "--repo-root", help="skill-manager repo root (default: auto-detect)")
    parser.add_argument("-p", "--project-root", help="project root for <agent>:local dirs (default: cwd)")


def _add_scope_flags(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-g", "--global", dest="scope", action="store_const", const="global",
                       help="bare agent names mean ~/.<agent>/skills (skip the g/l ask)")
    group.add_argument("-l", "--local", dest="scope", action="store_const", const="local",
                       help="bare agent names mean <project>/.<agent>/skills (skip the g/l ask)")


def _add_run_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-F", "--force", "--overwrite", dest="force", action="store_true",
                        help="overwrite targets that exist with different content")
    parser.add_argument("-n", "--dry-run", "--dry", dest="dry_run", action="store_true",
                        help="show what would happen without writing")
    parser.add_argument("-y", "--yes", action="store_true", help="skip confirmation prompts")
    parser.add_argument("-N", "--non-interactive", "--ni", dest="non_interactive", action="store_true",
                        help="never open interactive pickers")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="npm-style manager for agent skills: browse the skill-manager repo and\n"
                    "install into / save from local agent directories (claude, trae, codex, ...).",
        epilog=textwrap.dedent(
            """
            examples:
              skill list                          browse all collections with descriptions
              skill search lark                   find skills by keyword
              skill info merlin-job               metadata + where it is installed
              skill install merlin-job --to claude
              skill install -r merlin -a --to claude,codex
              skill install https://github.com/owner/repo --to claude
              skill uninstall merlin-job --from claude
              skill save my-skill --from claude --into global
              skill get https://github.com/google-deepmind/science-skills -a
              skill link agents claude,trae       share one skills dir across agents
              skill status                        compare installed skills with the repo
            """
        ).strip(),
    )
    parser.add_argument("-V", "--version", action="version", version=f"{PROG} {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p = sub.add_parser("list", aliases=["ls", "l"], help="list repo skills (or 'list local' for installed ones)")
    _add_common(p)
    p.add_argument("what", nargs="?", help="a collection name, or 'local' for installed skills")
    p.add_argument("-r", "--collection", dest="collection_opt", action="append", metavar="COLL",
                   help="list only these collections (repeatable, shorthands like 'lark' work)")
    p.add_argument("--all", action="store_true", help="with 'local': include empty directories")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("search", aliases=["find"], help="search skills by name/description keyword")
    _add_common(p)
    p.add_argument("keywords", nargs="+", metavar="keyword",
                   help="all keywords must match (name, collection, or description)")
    p.add_argument("-r", "--collection", dest="collection_opt", action="append", metavar="COLL",
                   help="search only these collections (repeatable, shorthands work)")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("info", aliases=["show"], help="show a skill's metadata and install locations")
    _add_common(p)
    p.add_argument("refs", nargs="+", metavar="skill", help="skill name or collection/name")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("install", aliases=["i", "in", "add"], help="install repo skills into agent directories")
    _add_common(p)
    p.add_argument("refs", nargs="*", metavar="skill",
                   help="skill name, collection/name, 'all', or a git URL (clones + imports, then installs)")
    p.add_argument("-t", "--to", action="append", metavar="TARGET",
                   help="agent dir: claude, trae:local, claude,codex, or a path (repeatable)")
    p.add_argument("-a", "--all", "--all-skills", dest="all", action="store_true",
                   help="all skills in the selected collection(s)")
    p.add_argument("-r", "--collection", "--repo", dest="collection_opt", action="append", metavar="COLL",
                   help="restrict to a collection, e.g. -r merlin (shorthands like 'lark' work; repeatable)")
    p.add_argument("-s", "--skills", dest="skills_opt", help=argparse.SUPPRESS)  # legacy comma list
    _add_scope_flags(p)
    _add_run_flags(p)
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("uninstall", aliases=["un", "rm", "remove"], help="remove installed skills from agent directories")
    _add_common(p)
    p.add_argument("refs", nargs="*", metavar="skill")
    p.add_argument("-f", "--from", dest="source", action="append", metavar="TARGET",
                   help="agent dir to remove from: claude, trae:local, ... (repeatable)")
    _add_scope_flags(p)
    p.add_argument("-n", "--dry-run", "--dry", dest="dry_run", action="store_true")
    p.add_argument("-y", "--yes", action="store_true")
    p.add_argument("-N", "--non-interactive", "--ni", dest="non_interactive", action="store_true")
    p.set_defaults(func=cmd_uninstall)

    p = sub.add_parser("save", aliases=["backup", "b"], help="save local agent skills back into a repo collection")
    _add_common(p)
    p.add_argument("refs", nargs="*", metavar="skill")
    p.add_argument("-f", "--from", dest="source", action="append", metavar="TARGET",
                   help="agent dir (claude, trae:local, ...) or a skill directory path")
    p.add_argument("-r", "--into", "--collection", "--repo", dest="into", metavar="COLL",
                   help="destination collection, e.g. merlin")
    p.add_argument("-a", "--all", "--all-skills", dest="all", action="store_true", help="all skills in the source dir")
    p.add_argument("-s", "--skills", dest="skills_opt", help=argparse.SUPPRESS)
    _add_scope_flags(p)
    _add_run_flags(p)
    p.set_defaults(func=cmd_save)

    p = sub.add_parser("get", aliases=["clone"], help="clone a GitHub/git repo into repos/ and import its skills into a collection")
    _add_common(p)
    p.add_argument("url", help="GitHub URL (supports /tree/<branch>/<subdir>), owner/repo shorthand, or any git URL")
    p.add_argument("refs", nargs="*", metavar="skill", help="skill names to import (default: pick interactively, or the only one found)")
    p.add_argument("-r", "--into", "--collection", dest="into", metavar="COLL",
                   help="destination collection (default: the repo name; created after confirmation / -y)")
    p.add_argument("-a", "--all", "--all-skills", dest="all", action="store_true", help="import every discovered skill")
    p.add_argument("-t", "--to", action="append", metavar="TARGET", help="also install the imported skills into agent dirs")
    _add_scope_flags(p)
    _add_run_flags(p)
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("link", aliases=["ln"], help="symlink agents' skills dirs to one shared source dir")
    _add_common(p)
    p.add_argument("refs", nargs="*", metavar="agent",
                   help="shared source first, then target agent(s): skill link agents claude,trae")
    _add_scope_flags(p)
    _add_run_flags(p)
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("status", aliases=["st"], help="compare installed skills against the repo (ok/modified/untracked)")
    _add_common(p)
    p.add_argument("-t", "--to", action="append", metavar="TARGET", help="only these agent dirs (repeatable)")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("doctor", aliases=["d"], help="show repo, collections, agent dirs, and launcher state")
    _add_common(p)
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("setup", aliases=["init"], help="install the `skill` launcher into ~/.local/bin")
    p.add_argument("-d", "--bin-dir", default="~/.local/bin", help="launcher directory (default: ~/.local/bin)")
    p.add_argument("--name", default=PROG, help=f"launcher name (default: {PROG})")
    p.add_argument("-F", "--force", action="store_true", help="replace launchers not created by this tool")
    p.set_defaults(func=cmd_setup)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not argv:
        parser.print_help()
        return 0
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        args.func(args)
    except CLIError as exc:
        print(red("error: ") + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print()
        return 130
    return 0
