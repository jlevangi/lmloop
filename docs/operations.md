# Running lmloop

## Starting and steering a run

```bash
cd ~/git/some-project
lmloop run "a broad objective"           # decomposed by the agent, not by you
lmloop run "..." --model llama-swap/local-wide --thinking low
lmloop run "..." --detach                # background; prints the run id
lmloop resume <run-id> --iterations 12   # continue, same worktree and plan
lmloop list                              # runs for this repo
lmloop status                            # what the newest run is doing now
```

Objectives should be **broad**. Narrowing by hand defeats the design: the agent
writes `plan.md` and works one step per iteration. If you find yourself writing
the steps, that is a bug in the prompt, not in the objective.

Control is by file, so it works from anywhere and survives the terminal going
away:

```bash
touch <run-dir>/PAUSE    # hold at the next iteration boundary
rm    <run-dir>/PAUSE    # carry on
touch <run-dir>/STOP     # finish the current iteration, commit, exit
touch <run-dir>/STOP-NOW # cut the current iteration short, commit, exit
```

Pausing mid-iteration is deliberately not offered: the model is mid-generation
and there is nothing honest to freeze.

Stopping mid-iteration is offered, under a different name. `STOP` waits for the
boundary — gate, checks, handoff, commit — which is where an hour of generation
turns into something the next run can start from, and is worth the wait unless
the iteration is visibly going nowhere. `STOP-NOW` is for when it is: pi is
killed where it stands and the partial tree is committed as `interrupted`.
Neither discards anything, so the choice is only about what you are willing to
wait for. `SIGINT` is `STOP-NOW`; a second `SIGINT` exits without committing.

## Run ids, and re-running the same objective

A run id is `<date>-<slug>-<hash-of-the-prompt>`, so the same objective on the
same day derives the same id — which is exactly the day it happens, because the
reason to re-run an objective is that the first attempt went nowhere. The second
attempt takes the next free name (`…-2`, `…-3`) rather than failing, and says
which run it stepped around:

```
lmloop 2026-08-19-tidy-the-thing-9add55-2
  note    2026-08-19-tidy-the-thing-9add55 exists; this is a separate attempt
          lmloop resume 2026-08-19-tidy-the-thing-9add55  to continue that one instead
```

Nothing is reused and nothing is removed: the earlier run keeps its worktree,
its branch and its handoff chain. A name counts as taken if *either* half of it
is — the worktree directory or the `lmloop/<run-id>` branch — so a worktree
removed by hand does not leave a branch behind for `git worktree add` to trip
over later.

## The gate

The gate command runs after every iteration, and once before the first one,
inside the **worktree** — not the repo root. Three outcomes are kept apart
because they ask for three different things:

| Result | Means | What happens |
| --- | --- | --- |
| `pass` | the gate ran and succeeded | nothing |
| `fail (rc=N)` | the gate ran and the code failed it | recorded; blocks the commit only if `blocks_commit` is set |
| `misconfigured (rc=127…)` | the shell could not find the command | the run refuses to start, and never blocks a commit |

The third row is why the probe exists. A gate of `.venv/bin/python -m compileall
-q backend` recorded `fail (rc=127)` on every one of a twelve-iteration run
because the worktree had no `.venv`, and nothing surfaced it but the event log —
the code was fine the whole time. It now stops at run start, naming the cwd.

A gate that fails identically on the base commit is reported as such, in the
terminal and in the agent's prompt, so an inherited failure does not read as one
the iteration caused.

## Where a run lives

```
<repo>/.worktrees/<run-id>/            # never deleted automatically
    .lmloop/runs/<run-id>/
        prompt.md          the objective, verbatim
        plan.md            the decomposition, maintained by the agent
        handoff.md         rewritten each iteration
        notes.md           per-iteration log
        status.json        the present, rewritten every 2s
        lmloop.log         JSONL event stream
        iteration-<n>.jsonl        raw pi events (megabytes)
        iteration-<n>-prompt.md    exactly what was sent
        sessions/          pi transcripts, replayable with `pi --session`
```

`status.json` is the file to read for "what is happening now". The event log is
append-only history; parsing it to the end is what the old dashboard did and why
it needed a cache.

It also carries what the *model* is doing, which on local hardware is a separate
question from what the agent is doing:

| field | meaning |
|---|---|
| `model` | the model running this iteration, which is not always the configured one -- planning and thrash retries use others |
| `thinking`, `role` | the settings behind that choice |
| `tokens_per_second` | output tokens per second, measured between message ends over the last five minutes |
| `input_tokens` | the prompt as the model counted it |
| `context_window` | what lmloop believes is safe for this model, or `0` if it has never been measured |
| `max_output_tokens` | the per-reply cap, which is what a `truncated` outcome ran into |

`tokens_per_second` is windowed rather than cumulative on purpose. A cumulative
average divides by every second the iteration spent running tools and waiting on
llama-swap to load a model, and so reports a speed the model never produced.
The windowed figure is the slope between message ends, which is the number that
answers "is it worth waiting for this".

## The dashboard

```bash
lmloop web        # 127.0.0.1:8082 by default
```

Deployed here as a systemd user unit on **:8766**, reachable at
`https://lmloop.example.com`, authenticating against the `lmloop-web` Keycloak
client.

```bash
systemctl --user status lmloop-web
journalctl --user -u lmloop-web -f
```

Config lives in `~/.config/lmloop/web.env` (mode 600). Without OIDC configured
the server **refuses to bind anything but loopback** — it has a launch button,
and that does not belong on a network unauthenticated.

## Testing changes without waiting hours

Local models make a real loop take hours. For anything that is not
model-behaviour work, drive it with a fast cloud model on a scratch repo:

```bash
T=/tmp/lmtest && rm -rf $T && mkdir -p $T/src && cd $T
git init -q && git config user.email t@t && git config user.name test
printf 'def add(a, b):\n    return a + b\n' > src/calc.py
printf '[agent]\nmodel = "9router/agent-smart"\n' > .lmloop.toml
git add -A && git commit -qm initial
lmloop run "Add a unittest suite covering every function in src/" --max-iterations 4
```

Iterations finish in about a minute and exercise every code path except
llama-swap preflight.

For display work, force a narrow terminal — this is the phone case and how the
wrapping bug was found:

```bash
script -qec "stty cols 32 rows 20; lmloop run '...' --max-iterations 1" out.log
sed 's/\x1b\[[0-9;]*[a-zA-Z]//g' out.log | tr '\r' '\n' | tail -20
```

## Reviewing what a run produced

Structural cleanliness is not correctness. Check both:

```bash
cd <worktree>
git log --oneline $(cat .lmloop/runs/*/base-commit)..HEAD
git diff --stat $(cat .lmloop/runs/*/base-commit)..HEAD
```

Then **run the thing**. Every real bug this project has found surfaced from
running it, never from reading it — and for frontend work, "every file parses"
told us nothing about 47 selectors that had stopped being loaded.

## Notifications

`[notify] url` (ntfy) pushes one message when a run stops. `dashboard_url`
makes it tap through to that run via the hash route the dashboard already
supports.

Deliberately one push at the end, not per iteration. The title carries the
verdict; no commits raises the priority, because a run that spent hours and left
git with nothing is the case worth being interrupted for.

## Disk

```bash
lmloop prune --dry-run
lmloop prune --roots ~/git --older-than 7
```

Runs are large: ~86 MB each, of which ~81 MB is `iteration-*.jsonl` — pi's raw
stream, 88% single-token `message_update` deltas. Plus a `pycache` directory
that reached 105 MB on one repo, because the gate and the agent's own commands
compile the whole virtualenv into it.

`prune` gzips the streams (≈97% saved; `rundir.open_iteration` reads either
form, so the compaction harvest still works) and deletes the bytecode cache,
which is the one thing in a run directory that carries no record. It refuses to
touch a run whose `status.json` is less than five minutes old.

It also runs when a run ends (`[prune] after_run`, default on), sweeping the
whole repository including the run that just finished — the liveness test is the
age of `status.json`, which would otherwise skip precisely the run holding the
space, so the loop vouches for its own id.

Nothing else is ever removed, and no worktree is ever deleted — a run that
produced nothing still has to be diagnosable.

## Merging

```bash
cd <repo>
git merge lmloop/<run-id>
```

Nothing is merged automatically. A run produces a branch; what happens to it is
yours to decide.
