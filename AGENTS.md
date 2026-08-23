# lmloop — notes for agents working on this repo

A loop that hands one objective to a local model, works on it for hours in a git
worktree, and commits what it actually did. ~2,600 lines, Python 3.11+, standard
library only, no build step.

**Read `docs/design.md` before changing behaviour.** Most of what looks arbitrary
in this codebase is load-bearing and was paid for with a failed run.

## The invariants — do not break these

1. **Nothing is ever discarded.** No `git reset --hard`, no `git clean`, no
   automatic worktree removal, anywhere. A run that produced nothing still has
   to be diagnosable.
2. **Git is the only witness.** Not iteration counts, not the agent's summary,
   not tool-call counts — the write counter under-counts by design.
3. **The handoff is a file the agent writes,** never a message the loop parses.
4. **The status line must fit the terminal.** This is correctness: an overlong
   line wraps and turns every refresh into a new scrolled line.

## Things that will waste your time if you rediscover them

- **`pi --mode json` always exits 0.** The branch setting `exitCode = 1` sits
  inside `if (mode === "text")`. Outcome comes from the event stream or nowhere.
- **Never pass `--no-extensions`.** `~/.pi/agent/extensions/model-catalog.js`
  registers both the `llama-swap` and `9router` providers; disabling extension
  discovery takes the whole model catalogue with it.
- **pi sets `process.title = "pi"`.** `pkill -f 'pi --model …'` never matches;
  use `pkill -x pi`.
- **llama-swap holds one model at a time.** `GET /running` is free;
  `GET /upstream/<model>/props` *causes* the swap it was meant to observe.
- **A cold model load takes ~4 minutes and emits nothing,** which is why the
  stall clock does not start until the first message or tool event.
- **The gate runs with `cwd` = the worktree,** not the repo — so a gate script
  must be tracked, or it will not exist there.
- **`[hidden]` loses to any `display` rule** in the dashboard's CSS, and a
  block-level progress fill with no width renders as 100%. Both shipped once.
- **Two different projects answer to "oh-my-pi".** The npm package is a pi
  extension and arrives through `PiHarness`. `omp` is
  `github.com/can1357/oh-my-pi`, a fork with its own binary — that is the one
  `OmpHarness` is for.
- **omp is not pi with a different name.** It has no `--session-id` (exit 2),
  its compaction events are `auto_compaction_*`, its `edit` takes a patch script
  rather than a path, and it *rejects* `--tools` names it does not know instead
  of ignoring them — which is one of the few ways `--mode json` exits non-zero.
- **omp's browser cannot carry a query-string credential.** It takes an HTTP CDP
  discovery endpoint, rejects `ws://`, and drops `?token=` on both the paths it
  uses to reach one. `browser.py` says so before a run rather than after.

## Working on this

```bash
python3 -m compileall -q . web/
python3 -m unittest test_lmloop     # policy, adapters, allowlists; no agent needed
lmloop run "..." --max-iterations 1 # on a scratch repo, with a cloud model
```

See `docs/operations.md` for the scratch-repo recipe that exercises every code
path in about a minute, and the narrow-terminal recipe for display work.

**Verify by running the thing.** Every real bug this project has found surfaced
from running it, never from reading it — including four in one session, and a
CSS regression that no amount of file-reading revealed because every file parsed
perfectly.

## Documentation

| Document | For |
|---|---|
| `docs/design.md` | why the invariants exist |
| `docs/failure-modes.md` | how runs fail, with the evidence for each |
| `docs/models.md` | measured local-model behaviour and window budgets |
| `docs/architecture.md` | what each module does |
| `docs/operations.md` | running, steering, reviewing, deploying |

## Scope

lmloop is general. It drives `pi` and nothing else, and it must work on any
repository and any task. Checks in `checks.py` ask only "did the edit land
intact"; anything encoding what a *particular* project considers correct belongs
in that project's own `.lmloop.toml` gate.

---

# Agent Instructions

This project uses **bd** (beads) for issue tracking. Run `bd prime` for full workflow context.

> **Architecture in one line:** Issues live in a local Dolt database
> (`.beads/dolt/`); cross-machine sync uses `bd dolt push/pull` (a
> git-compatible protocol), stored under `refs/dolt/data` on your git
> remote — separate from `refs/heads/*` where your code lives.
> `.beads/issues.jsonl` is a passive export, not the wire protocol.
>
> See [SYNC_CONCEPTS.md](https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md)
> for the one-screen overview and anti-patterns (don't treat JSONL as the
> source of truth; don't `bd import` during normal operation; don't
> reach for third-party Dolt hosting before trying the default).

## Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work atomically
bd close <id>         # Complete work
bd dolt push          # Push beads data to remote
```

## Non-Interactive Shell Commands

**ALWAYS use non-interactive flags** with file operations to avoid hanging on confirmation prompts.

Shell commands like `cp`, `mv`, and `rm` may be aliased to include `-i` (interactive) mode on some systems, causing the agent to hang indefinitely waiting for y/n input.

**Use these forms instead:**
```bash
# Force overwrite without prompting
cp -f source dest           # NOT: cp source dest
mv -f source dest           # NOT: mv source dest
rm -f file                  # NOT: rm file

# For recursive operations
rm -rf directory            # NOT: rm -r directory
cp -rf source dest          # NOT: cp -r source dest
```

**Other commands that may prompt:**
- `scp` - use `-o BatchMode=yes` for non-interactive
- `ssh` - use `-o BatchMode=yes` to fail instead of prompting
- `apt-get` - use `-y` flag
- `brew` - use `HOMEBREW_NO_AUTO_UPDATE=1` env var

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:6cd5cc61 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->

<!-- BEGIN BEADS CODEX SETUP: generated by bd setup codex -->
## Beads Issue Tracker

Use Beads (`bd`) for durable task tracking in repositories that include it. Use the `beads` skill at `.agents/skills/beads/SKILL.md` (project install) or `~/.agents/skills/beads/SKILL.md` (global install) for Beads workflow guidance, then use the `bd` CLI for issue operations.

### Quick Reference

```bash
bd ready                # Find available work
bd show <id>            # View issue details
bd update <id> --claim  # Claim work
bd close <id>           # Complete work
bd prime                # Refresh Beads context
```

### Rules

- Use `bd` for all task tracking; do not create markdown TODO lists.
- Run `bd prime` when Beads context is missing or stale. Codex 0.129.0+ can load Beads context automatically through native hooks; use `/hooks` to inspect or toggle them.
- Keep persistent project memory in Beads via `bd remember`; do not create ad hoc memory files.

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.
<!-- END BEADS CODEX SETUP -->
