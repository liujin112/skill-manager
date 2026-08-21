---
name: skill-manager
description: Operate the `skill` CLI (skill-manager) to manage agent skills for the user. Use when the user asks to install/uninstall/search agent skills, save a local skill into their skill repo, import skills from a GitHub repo, share one skills directory across agents (claude/codex/cursor/trae), check whether installed skills drifted from the repo, or mentions skill-manager, skillctl, or the skill command. Chinese triggers include 装skill、安装技能、保存skill、备份skill、从github装skill、同步技能、技能仓库、更新skill.
---

# skill-manager — agent usage

`skill` is an npm-style CLI managing a git library of agent skills. Library
layout: `projects/<collection>/skills/<skill>/SKILL.md`. Skills install into
agent dirs like `~/.claude/skills` (global) or `<project>/.claude/skills`
(local).

## Rules for agents

1. **Never trigger the interactive pickers** — they need a raw TTY. Always
   pass explicit skill names plus `-N -y`, and the flags that pre-answer every
   question: `--to/-t`, `--from/-f`, `-r` (collection), `-g`/`-l` (scope).
2. Preview destructive or bulk operations with `-n` (dry-run) first when
   unsure; `uninstall` in non-interactive mode requires `-y`.
3. If `skill` is not on PATH, find the repo (commonly `*/skill-manager` or
   `*/all_skills`) and run `./install.sh`, or call `<repo>/bin/skill` directly.
   `skill doctor` shows the repo root, collections, agent dirs, and launcher.
4. Existing targets with different content are skipped unless `-F` — report
   that to the user instead of forcing, unless they asked to overwrite.

## Command map

| Task | Command |
| --- | --- |
| Browse library | `skill list` · `skill list -r <coll>` · `skill list local` |
| Find a skill | `skill search <keyword...>` (all must match; `-r <coll>` narrows) |
| Inspect | `skill info <name>` (metadata, size, install locations, drift) |
| Install | `skill install <name> --to claude -N -y` |
| Install a collection | `skill install -r <coll> -a --to claude,codex -N -y` |
| Install from GitHub | `skill install <git-url> -a --to claude -N -y` |
| Import from GitHub only | `skill get <git-url> -a -N -y` |
| Save local skill to repo | `skill save <name> --from claude -r <coll> -N -y` |
| Remove | `skill uninstall <name> --from claude -N -y` |
| Drift check | `skill status` (ok / modified / untracked) |
| Share dir across agents | `skill link agents claude,codex -N -y` |
| Update everything | `skill update -n` to preview, then `skill update` |
| Commit + push the library | `skill sync` (or `skill sync -m "<message>"`) |

## Details that matter

- **Names**: a bare name resolves across collections; on ambiguity the error
  lists candidates — retry with `<collection>/<name>`.
- **Targets**: `<agent>[:scope]`, agents `claude/trae/codex/cursor/agents`,
  scope `global` (default, `~/.<agent>/skills`) or `local`
  (`<project>/.<agent>/skills`; project root = cwd, override with `-p`).
  Comma-separate for multiple. Plain directory paths also work. In `-N` mode a
  bare agent name silently means global — pass `-l` or `:local` for local.
- **GitHub**: `skill get`/`install <url>` clones under `<repo>/repos/`,
  discovers every dir containing SKILL.md (repo root counts), imports into a
  collection named after the repo (`-r` overrides; `-y` auto-creates), and
  records provenance in `projects/<coll>/.source.json`. `/tree/<branch>/<dir>`
  URLs restrict the scan. Re-run the same command to pull upstream updates.
- **save**: `-y` auto-creates a missing destination collection. `--from` may
  also be a path to one skill directory.
- **link** replaces an agent's whole skills dir with a symlink to a shared
  dir. Skills unique to the target are merged into the shared dir first;
  conflicting local edits abort the link — `skill save` them, or `-F` discards
  them. Never `-F` a link when the refusal mentions non-skill entries (e.g.
  trae's `.system`) — that would delete agent-internal data.
- **status → fix**: `modified` → keep edits with `skill save <name> --from
  <agent> -r <coll> -F -N -y`, or restore with `skill install <name> --to
  <agent> -F -N -y`. `untracked` → adopt with `skill save`.
- **update** pulls in three steps: the tool checkout (blocked by unpushed
  commits or local edits — say so instead of forcing), each imported
  collection from the URL in its `.source.json` (only when upstream moved),
  and then installed copies of skills that changed, into the agent dirs that
  already had them. Locally edited copies are skipped — report them; use `-F`
  only when the user wants their edits discarded. `skill update <collection>`
  narrows it; `--no-tool` / `--no-install` skip a step.
- **sync** is the push side: stage everything, commit with a generated message
  (`-m` overrides), `pull --rebase`, push. A conflicting rebase aborts itself
  and needs the user. `--public` also runs `scripts/sync_public.sh`.
- Neither `update` nor `sync` opens a picker, so they need no `-N -y`; prefer
  `-n` first for a preview.
- After changing the library (`save`/`get`), offer to run `skill sync` so the
  new skills are committed and pushed.
