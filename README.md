# skill-manager

An npm-style command-line manager for agent skills — the `SKILL.md` folders
used by Claude Code, Codex, Cursor, Trae and other coding agents.

Keep all your skills in one git repo, organized into **collections**, and
install them into whichever agent needs them — instead of copy-pasting skill
folders between `~/.claude/skills`, `~/.codex/skills`, and every project.

```text
your-skills-repo/
├── bin/skill            # this CLI
├── lib/skillcli/
├── install.sh
└── projects/            # your skill library, one folder per collection
    ├── writing/skills/...
    ├── research/skills/...
    └── team-tools/skills/...
```

## Install

```bash
git clone https://github.com/liujin112/skill-manager.git
cd skill-manager && ./install.sh     # installs `skill` into ~/.local/bin
```

The repo you clone is also your library: import or create collections under
`projects/` and commit them. To keep the library elsewhere, set
`SKILL_REPO_ROOT=/path/to/your/library`.

## Quick start

```bash
skill list                            # browse collections with descriptions
skill search pdf                      # search by name/description keyword
skill info some-skill                 # metadata + where it is installed

skill install some-skill --to claude  # name resolves across collections
skill install writing/some-skill      # or address it as collection/name
skill install -r research -a --to claude,codex   # whole collection, two agents

skill install https://github.com/google-deepmind/science-skills/tree/main/skills -a --to claude
                                      # one step: clone -> import -> install

skill status                          # ok / modified / untracked vs the repo
skill save my-skill --from claude -r team-tools  # adopt a local skill into the repo
skill uninstall some-skill --from claude
```

Run `install` / `save` / `uninstall` with no arguments for interactive pickers
— clack-style prompts that redraw in a small window and collapse to a one-line
summary once answered. Keys: `Space` select, `a` all, `/` live filter (handy
when an imported repo has 30+ skills; `a` then selects all matches), `Enter`
confirm, `q` cancel.

## Commands

| Command | What it does |
| --- | --- |
| `skill list [collection\|local]` | List repo skills with descriptions, or installed ones |
| `skill search <keyword...>` | Search names and descriptions (all keywords must match; `-r` narrows) |
| `skill info <name>` | Frontmatter, size, install locations and drift |
| `skill install [name...\|url]` | Repo → agent dirs; identical re-installs are `up-to-date` |
| `skill uninstall <name...>` | Remove installed skills from agent dirs |
| `skill save [name...]` | Agent dir → repo collection (adopt local skills) |
| `skill get <git-url>` | Clone a repo into `repos/`, import its skills as a collection |
| `skill link <src> <agents...>` | Symlink agents' skills dirs to one shared dir |
| `skill update [collection...]` | Pull: the tool checkout, imported collections' upstreams, installed copies |
| `skill sync` | Push: commit local changes, `pull --rebase`, push the library repo |
| `skill status` | git-status-like view: ok / modified / untracked |
| `skill doctor` | Show repo, collections, agent dirs, launcher state |
| `skill setup` | (Re)install the launcher |

## Agents and scopes

`--to` / `--from` accept `<agent>[:scope]` with agents `claude / trae / codex /
cursor / agents`, scope `global` (`~/.<agent>/skills`, default) or `local`
(`<project>/.<agent>/skills`). Multiple targets: `--to claude,codex`. Plain
paths work too. A bare agent name asks `[G/l]` once on a TTY; pin it with the
`-g/--global` or `-l/--local` flag, or set `SKILL_DEFAULT_TARGET`.

## Installing from GitHub

```bash
skill get https://github.com/owner/repo                  # any SKILL.md layout
skill get https://github.com/owner/repo/tree/main/skills # branch + subdir links
skill get owner/repo -a                                  # shorthand
```

Clones are cached as `repos/<owner>--<repo>` (e.g. `anthropics--skills`), so
same-named repos from different authors never collide.
A `SKILL.md` at the repo root means the whole repo is one skill; otherwise
every directory containing a `SKILL.md` is discovered. Skills are imported
into a collection named after the repo (`-r` to override), with provenance
written to `projects/<collection>/.source.json` (URL, commit, import time) —
your copy survives even if the upstream repo disappears. Re-run the same
`skill get` to pull updates.

## Sharing one skills dir across agents

```bash
skill link agents claude,trae   # ~/.claude/skills and ~/.trae/skills become
                                # symlinks to ~/.agents/skills
```

Whole-directory symlinks: install once into the shared dir and every linked
agent sees it immediately. Skills unique to a target are merged into the
shared dir first, so nothing is lost; undo by deleting the symlink.

## Staying up to date

Your library is a git repo, so keeping it current should not mean `cd`-ing
into it and running git by hand:

```bash
skill update            # pull the tool, refresh imported skills, update installs
skill update -n         # report only: what is behind upstream
skill sync              # commit local changes, pull --rebase, push
skill sync -m "add pdf-tools"
```

`skill update` runs three steps and reports each one:

1. **The tool** — fast-forwards this checkout and shows the version change.
   Unpushed commits or uncommitted edits block the merge; it tells you to run
   `skill sync` instead of touching your work.
2. **Imported collections** — updates each cached clone under `repos/` from the
   URL in its `.source.json`, and re-imports the recorded skills only when the
   upstream commit actually moved. Skills that appeared upstream since your
   import are reported, never installed behind your back; ones you deleted
   locally stay deleted.
3. **Installed copies** — skills that changed in this run are copied into every
   agent directory that already had them (shared symlinked dirs are handled
   once). Copies you edited yourself are skipped and listed; `-F` overwrites
   them and re-syncs any other drift.

`skill sync` stages everything, writes a commit message from the diff
(`Update skills (3 added, 1 updated)`, one `+ collection/name` line per skill),
rebases onto the remote, and pushes — setting the upstream on a first push. A
conflicting rebase is aborted so your checkout is never left mid-rebase. Use
`-n` to preview, `--no-pull` / `--no-push` to run half of it, and `--public` if
you keep a `scripts/sync_public.sh` mirror script.

## Let your agent drive it

An agent-facing skill lives at `skills/skill-manager/` — it teaches Claude
Code / Codex / Cursor / Trae how to run this CLI non-interactively. Import and
install it with the CLI itself:

```bash
skill install https://github.com/liujin112/skill-manager --to claude
```

## Environment variables

- `SKILL_REPO_ROOT` — library location (default: the repo the tool lives in)
- `SKILL_DEFAULT_TARGET` — default `--to` for `skill install`
- `NO_COLOR` — disable colored output

## License

MIT
