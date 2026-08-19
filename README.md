# lmloop

A loop that hands one objective to a local model, works on it for hours in a git
worktree, and commits what it actually did.

```bash
lmloop run "refactor the dashboard stat grid into two rows"
```

It drives [`pi`](https://github.com/earendil-works) and nothing else. Tools,
skills, models and prompts belong to the agent; the loop's only jobs are
isolation, iteration, and never throwing work away.

## Documentation

| Document | For |
|---|---|
| [docs/design.md](docs/design.md) | the invariants and why each one exists |
| [docs/failure-modes.md](docs/failure-modes.md) | how runs fail, with the evidence for each |
| [docs/models.md](docs/models.md) | measured local-model behaviour and window budgets |
| [docs/architecture.md](docs/architecture.md) | what each module does |
| [docs/operations.md](docs/operations.md) | running, steering, reviewing, deploying |

`AGENTS.md` is the short version for an agent working on this repo.

## Why this exists

Existing loops are tuned for fast, reliable cloud agents, where a discarded
iteration costs ninety seconds and redoing it is cheaper than reasoning about
it. At ninety minutes an iteration that trade inverts. One run produced 87
minutes of correct, coherent edits and lost every line because the agent's final
message was not parseable JSON. The code was fine; the envelope was not.

So `lmloop` makes three commitments:

**Nothing is ever discarded.** There is no `git reset --hard` in this codebase.
Every iteration that leaves a diff produces a commit, labelled with what
happened — `ok`, `timeout`, `stalled`, `thrashing`, `no-action`, `interrupted`,
`agent-error`. Failed work
becomes a labelled commit you can revert on your own schedule, not a hole in the
history.

**The handoff is a file, not a parsed message.** The agent writes `handoff.md`
with its own write tool. There is no envelope to fail to parse. If it never
writes one, the loop synthesises one from `git diff` and marks the iteration
degraded — never discarded. If the iteration overflowed its context, the loop
harvests the summary the agent wrote for itself on the way out and carries that
forward instead: an agent that ran out of room did write a handoff, just into
pi's event stream rather than to disk.

**Git is the only witness.** Not iteration counts, not the agent's summary, not
tool-call counts. An agent once reported twelve successful iterations across 479
tool calls and zero writes. Another edited files perfectly well through its bash
tool while the write-tool counter read zero. `git diff` against the run's base
commit is the only honest progress signal, and it is what the stop conditions
and the change guard read.

## Install

```bash
git clone <this repo> ~/git/lmloop
printf '#!/usr/bin/env bash\nexec python3 "$HOME/git/lmloop/lmloop.py" "$@"\n' > ~/.local/bin/lmloop
chmod +x ~/.local/bin/lmloop
lmloop init            # writes ~/.config/lmloop/config.toml
```

Python 3.11+ (for `tomllib`), stdlib only, no build step.

## Use

```bash
cd ~/git/some-project
lmloop init --project                      # optional ./.lmloop.toml
lmloop run "the objective" --max-iterations 5
lmloop run "the objective" --model llama-swap/local-wide
lmloop models                              # what is loaded, measured, selectable
lmloop models --detect                     # measure the loaded model's real context
```

Stop a run, at the boundary or right now:

```bash
touch <worktree>/.lmloop/runs/<run-id>/STOP      # after this iteration
touch <worktree>/.lmloop/runs/<run-id>/STOP-NOW  # cut this iteration short
```

`STOP` lets the current iteration finish, so the boundary can do its work — gate,
checks, handoff, commit — and only then does the run exit. That is worth waiting
for: the handoff is what makes the hour reusable. `STOP-NOW` is for an iteration
that is visibly wasting its hour; pi is killed where it stands, the partial tree
is still committed, and the iteration is recorded as `interrupted`. Neither
discards anything. `SIGINT` means `STOP-NOW`, because that is what asking for
your terminal back means; a second `SIGINT` exits without committing.

## What a run leaves behind

A worktree at `<repo>/.worktrees/<run-id>/` on branch `lmloop/<run-id>`, which is
**never deleted automatically** — a run that produced nothing still has to be
diagnosable. Inside it:

```
.lmloop/runs/<run-id>/
    prompt.md                the objective, verbatim
    base-commit              what progress is measured against
    notes.md                 per-iteration log
    handoff.md               rewritten each iteration by the agent
    lmloop.log               JSONL event stream
    iteration-<n>.jsonl      raw pi events
    iteration-<n>-prompt.md  exactly what was sent
    gate-<n>.log             gate output
    sessions/                pi transcripts, replayable with `pi --session`
```

Review and merge:

```bash
git log --oneline <base>..lmloop/<run-id>
git merge lmloop/<run-id>
```

## The dashboard

```bash
lmloop web            # http://127.0.0.1:8082
```

Start a run, watch it, pause it, stop it, and continue a finished one, across
every project under `LMLOOP_WEB_ROOTS` — without an ssh session or a tmux
attach. It is phone-first, installable to a home screen, and shows plan progress
rather than iteration counts, because "4 of 10 steps" is the honest measure of a
long run and "iteration 7 of 20" is not.

Two properties make it small. **Runs are controlled by files**, so pausing is
`touch PAUSE` and the dashboard never owns a run's lifecycle: it can crash, be
restarted, or be replaced mid-run and every control still works. And **the
present is a file too** — `status.json` — so it reads state instead of replaying
an append-only log to its end.

A run that dies is reported as `stale`, never as running. A crashed loop leaves
a `status.json` that still says `working`, so liveness comes from the age of that
file and from whether the log records a completion.

Without OIDC configured the server binds loopback only, and says so rather than
quietly listening on every interface. A dashboard with a launch button does not
belong on a network unauthenticated. To expose it, set the `LMLOOP_WEB_OIDC_*`
variables in `~/.config/lmloop/web.env` (see `web/deploy/web.env.example`) and
install its only two dependencies for the interpreter that runs it:

```bash
/usr/bin/python3 -m pip install "PyJWT[crypto]>=2.7,<3" "requests>=2.31,<3"
```

lmloop itself stays stdlib-only: those are imported in `web/auth.py` and nowhere
else, and a missing one disables authentication rather than breaking the loop.

`web/deploy/lmloop-web.service` runs it under systemd.

## Checks

Every iteration, whatever the project configured, the files git says changed are
checked for damage an *edit* does rather than for whether the program is
correct: a file that stops parsing (Python, JSON, TOML, CSS braces, JS), a
conflict marker, or a block of lines pasted twice. When something fails, repair
becomes the next iteration's job and the plan waits.

It is deliberately not a linter — style is the agent's business, and a noisy
check is one that gets ignored. Anything encoding what a *particular* repository
considers correct belongs in that repository's own `[gate] command`.

## Notifications

A run is unattended for hours by design, so the moment it ends is the moment
nobody is watching.

```toml
[notify]
url           = "https://ntfy.example.com"
topic         = "lmloop"
dashboard_url = "https://lmloop.example.com"   # taps through to the run
```

One push when the run stops, never per iteration — a notification every twenty
minutes for ten hours is a channel you learn to ignore. The title carries the
verdict, because that is what a lock screen shows: *"one-project: 9 commits"*,
or *"one-project: nothing committed"* at high priority, which is the case worth
interrupting someone for.

Empty `url` disables it, and it can never fail a run.

## Disk

A run costs about 86 MB, almost all of it pi's raw event stream, and a bytecode
cache that reached 105 MB on one repository. Left alone this fills a disk.

```bash
lmloop prune --dry-run          # what it would do
lmloop prune                    # this repo's finished runs
lmloop prune --roots ~/git --older-than 7
```

It **deletes no record**. Event streams are gzipped, which saves about 97% on a
file that is 88% single-token deltas, and everything reads either form. The only
thing removed is the bytecode cache, which is derived from source that is still
present and records nothing about what the agent did. A run still writing is
skipped.

On one-project: 313 MB → 8.6 MB, with every stream still readable.

This also runs automatically when a run ends — `[prune] after_run`, on by
default. A run is exactly when the space appears and when someone is watching,
which is easier to trust than a cron job quietly rewriting run directories at
3am. It prints one line saying what it freed.

## Configuration

`~/.config/lmloop/config.toml`, overridden by `.lmloop.toml` in the repo. See
`lmloop init` for a commented starting point. The parts that matter:

- `[iteration] timeout_seconds` — a backstop, in hours not minutes. The real
  protection is `stall_seconds`, which fires when the agent stops emitting
  anything. The stall clock does not start until the first event arrives,
  because llama-swap may legitimately spend minutes swapping models first.
- `[iteration] max_compactions` — give up on an iteration that has overflowed
  its context this many times without writing anything. An agent whose window is
  smaller than the codebase can spend the whole iteration reading a dozen files,
  overflowing, and reading them again: observed on one-project at six overflows
  in 69 minutes across 81 tool calls, none of them a write. Cutting it off is
  free, because whatever the iteration left behind is committed either way.
- `[worktree] link` — untracked paths symlinked from the repo into each new
  worktree, so the agent gets the environment and not just the source.
  `git worktree add` materialises tracked files only, which leaves a Python
  project without its virtualenv and a Node project without `node_modules`.
  Watched live: an agent spent an hour and 24 tool calls enumerating every
  `python3` on the box looking for one that could import Flask, while the
  project's own `.venv` sat unreachable in the repo above it. The linked names
  are added to the git exclude list, and the prompt names the interpreter — an
  agent that does not know the environment is there goes looking for it.
  `.env` is deliberately not a default: add it per project if the code needs
  it, rather than having the loop hand a model your secrets uninvited.
- `[agent] planner_model` — a different model for the iteration that writes the
  plan. Deciding the steps is a whole-repository question that happens once and
  wants the widest window; carrying one out happens every iteration and wants
  throughput. Empty uses `model` for both.
- `[gate] command` — run after every iteration. `blocks_commit = false` records
  the result in the commit message and the next iteration's prompt but commits
  regardless, which is usually what you want.
- `[stop] no_diff_iterations` — stop after N iterations that git says changed
  nothing. This is the guard that catches an agent confidently going nowhere.

## Notes on local models

- Use `llama-swap/<model>`, not a router alias. A router reports model metadata,
  not how the weights were loaded; one advertised 1,000,000 context for a model
  running with `--ctx-size 65536`, and declaring that killed runs with HTTP 400
  mid-iteration.
- `lmloop models --detect` reads the real `--ctx-size` off the running
  llama-server command line and caches it to
  `~/.config/lmloop/model-context.json`. It only measures what is already
  loaded: llama-swap holds one model at a time, so probing an unloaded model
  *causes* the swap it was meant to observe.
- `[agent] tools` trims pi's tool allowlist. This matters more than it looks —
  a trivial prompt with only `read` enabled still costs ~7.5K input tokens of
  system prompt and extension overhead, which is 13% of a 57K declared window
  before any work happens.

## Running it as an attachable screen

A run is a foreground process with a live status line, not a background job:

```
  iteration 2: local-fast already loaded
  iter 2/3  41m18s  17 tools  12043 out  read
```

The bottom line keeps moving so an attached terminal never looks dead. It shows
the tool in flight, or how long the agent has been quiet. A model load is
reported once when it finishes (`model ready in 3m55s`) rather than repeated.

**The status line is always trimmed to one terminal line, and that is a
correctness requirement rather than tidiness.** A line wider than the terminal
wraps; `\r` then returns only to the start of the last visual line, so the clear
leaves the remnant behind and every refresh scrolls a new line. An overlong
status line does not look slightly wrong -- it turns the display into a spam
log. Segments are dropped by priority as the terminal narrows, so a phone
terminal degrades to what matters:

```
 60 |  2/3  41m18s  read  17 tools  12043 out|
 44 |  2/3  41m18s  read  17 tools  [STOP]|
 32 |  2/3  41m18s  read  [STOP]|
 24 |  2/3  read  [STOP]|
```

`<run-dir>/status.json` carries the same state as a small, atomically-replaced
document, so a phone script, a status bar, or a web wrapper can read what a run
is doing without parsing a megabyte event log:

```bash
lmloop status            # the most recent run
lmloop status --json     # the raw document
```

Controls are files first, keystrokes second — a file works from ssh, a phone, or
another script, and survives the terminal going away:

| key | file | effect |
|---|---|---|
| `p` | `touch <run-dir>/PAUSE` | hold after the current iteration |
| `r` | `rm <run-dir>/PAUSE` | carry on |
| `q` | `touch <run-dir>/STOP` | finish the current iteration, commit, exit |
| `Q` | `touch <run-dir>/STOP-NOW` | cut the current iteration short, commit, exit |

Pausing mid-iteration is deliberately not offered: the model is mid-generation
and there is nothing honest to freeze. The pause lands at the iteration
boundary, where the tree is committed and the handoff is written.

Stopping mid-iteration *is* offered, because the two stops answer different
questions. `q` is "I am done with this run" and waits for the boundary, where an
hour of work becomes a commit and a handoff. `Q` is "this iteration is wasting
its hour" and does not wait. The keys are separate so that neither is a surprise.

### From Paseo

Paseo workspace terminals host this directly, with no workspace registration:

```bash
paseo terminal create --cwd ~/git/one-project --name lmloop --json
paseo terminal send-keys <id> "lmloop run 'the objective' --max-iterations 3" Enter
paseo terminal capture   <id> --json      # what it is doing now
paseo terminal send-keys <id> p           # pause
paseo terminal send-keys <id> r           # resume
paseo terminal send-keys <id> q           # halt after this iteration
```

`capture` without `--json` renders only the visible region, which for a mostly
idle screen looks blank; `--json` returns the lines.

### From pi

`~/.pi/agent/prompts/lmloop.md` gives pi a `/lmloop <objective>` slash command.
It sharpens the objective and starts the run with `--detach`, returning the run
id immediately rather than blocking the session for hours. Use this to kick a
run off from a pi session; use a terminal when you want the screen.
