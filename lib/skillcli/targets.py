# Local agent skill directories: where agents (claude/trae/codex/cursor/agents)
# read skills from, either globally (~/.<agent>/skills) or per-project
# (<project>/.<agent>/skills). Resolves readable specs like "claude" or
# "trae:local", plus the legacy skillctl short aliases.
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List

from .registry import CLIError

# Also the display order in pickers, doctor, and status: agents/claude first.
AGENTS = ["agents", "claude", "trae", "codex", "cursor"]

# Short aliases from the old skillctl, kept working but no longer documented.
# All map to an explicit scope so they never trigger the interactive g/l ask.
_LEGACY = {
    "tg": "trae:global", "tl": "trae:local",
    "ag": "agents:global", "al": "agents:local",
    "cg": "codex:global", "cl": "codex:local",
    "claudeg": "claude:global", "claudel": "claude:local",
    "cursorg": "cursor:global", "cursorl": "cursor:local",
}


@dataclass(frozen=True)
class Target:
    agent: str
    scope: str  # "global" | "local" | "custom"
    path: Path

    @property
    def label(self) -> str:
        if self.scope == "custom":
            return str(self.path)
        return f"{self.agent}:{self.scope}"


def known_targets(project_root: Path) -> List[Target]:
    home = Path.home()
    targets: List[Target] = []
    for agent in AGENTS:
        targets.append(Target(agent, "global", home / f".{agent}" / "skills"))
        targets.append(Target(agent, "local", project_root / f".{agent}" / "skills"))
    return targets


def resolve_target(raw: str, project_root: Path) -> Target:
    """Accept 'claude', 'claude:local', 'claude-global', legacy aliases, or a path."""
    spec = _LEGACY.get(raw.strip().lower(), raw.strip().lower())
    for sep in (":", "-"):
        head, _, tail = spec.partition(sep)
        if head in AGENTS and tail in {"g", "global", "l", "local"}:
            scope = "local" if tail.startswith("l") else "global"
            return _lookup(head, scope, project_root)
    if spec in AGENTS:
        return _lookup(spec, "global", project_root)
    if "/" in raw or raw.startswith("~") or Path(raw).expanduser().exists():
        # abspath, not resolve(): keep symlinked dirs addressable as themselves
        return Target("path", "custom", Path(os.path.abspath(Path(raw).expanduser())))
    raise CLIError(
        f"unknown target '{raw}'; use <agent>[:local] with agent in "
        + "/".join(AGENTS) + ", or a directory path"
    )


def split_specs(values: List[str]) -> List[str]:
    """Flatten repeated/comma-separated option values into individual specs."""
    return [item.strip() for value in values for item in value.split(",") if item.strip()]


def is_bare_agent(raw: str) -> bool:
    """True for a plain agent name with no scope ('claude'), where the CLI may
    still ask global vs local; 'claude:local' and paths are already explicit."""
    return _LEGACY.get(raw.strip().lower(), raw.strip().lower()) in AGENTS


def parse_target_list(values: List[str], project_root: Path) -> List[Target]:
    """Flatten repeated/comma-separated --to values into unique targets."""
    targets: List[Target] = []
    for value in values:
        for item in value.split(","):
            if item.strip():
                target = resolve_target(item, project_root)
                if target not in targets:
                    targets.append(target)
    return targets


def _lookup(agent: str, scope: str, project_root: Path) -> Target:
    for target in known_targets(project_root):
        if target.agent == agent and target.scope == scope:
            return target
    raise CLIError(f"unknown target {agent}:{scope}")
