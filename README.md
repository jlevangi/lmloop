# lmloop

A loop that hands one objective to a local model, works on it for hours in a git
worktree, and commits what it actually did.

```bash
lmloop run "refactor the dashboard stat grid into two rows"
```

It drives [`pi`](https://github.com/earendil-works) and nothing else. Tools,
skills, models and prompts belong to the agent; the loop's only jobs are
isolation, iteration, and never throwing work away.

## Why this exists

Existing loops are tuned for fast, reliable cloud agents, where a discarded
iteration costs ninety seconds and redoing it is cheaper than reasoning about
it. At ninety minutes an iteration that trade inverts. One run produced 87
minutes of correct, coherent edits and lost every line because the agent's final
message was not parseable JSON. The code was fine; the envelope was not.

So `lmloop` makes three commitments:

**Nothing is ever discarded.** There is no `git reset --hard` in this codebase.
Every iteration that leaves a diff produces a commit, labelled with what
happened — `ok`, `timeout`, `stalled`, `interrupted`, `agent-error`. Failed work
becomes a labelled commit you can revert on your own schedule, not a hole in the
history.

**The handoff is a file, not a parsed message.** The agent writes `handoff.md`
with its own write tool. There is no envelope to fail to parse. If it never
writes one, the loop synthesises one from `git diff` and marks the iteration
degraded — never discarded.

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

Stop a run without killing it mid-write:

```bash
touch <worktree>/.lmloop/runs/<run-id>/STOP
```

The run finishes the current iteration, commits it, and exits. `SIGINT` does the
same thing; a second `SIGINT` is immediate.

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

## Configuration

`~/.config/lmloop/config.toml`, overridden by `.lmloop.toml` in the repo. See
`lmloop init` for a commented starting point. The parts that matter:

- `[iteration] timeout_seconds` — a backstop, in hours not minutes. The real
  protection is `stall_seconds`, which fires when the agent stops emitting
  anything. The stall clock does not start until the first event arrives,
  because llama-swap may legitimately spend minutes swapping models first.
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
the model loading, the tool in flight, or how long the agent has been quiet.

Controls are files first, keystrokes second — a file works from ssh, a phone, or
another script, and survives the terminal going away:

| key | file | effect |
|---|---|---|
| `p` | `touch <run-dir>/PAUSE` | hold after the current iteration |
| `r` | `rm <run-dir>/PAUSE` | carry on |
| `q` | `touch <run-dir>/STOP` | finish the current iteration, commit, exit |

Pausing mid-iteration is deliberately not offered: the model is mid-generation
and there is nothing honest to freeze. The pause lands at the iteration
boundary, where the tree is committed and the handoff is written.

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
