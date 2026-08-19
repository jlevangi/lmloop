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
```

Pausing mid-iteration is deliberately not offered: the model is mid-generation
and there is nothing honest to freeze.

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

## Merging

```bash
cd <repo>
git merge lmloop/<run-id>
```

Nothing is merged automatically. A run produces a branch; what happens to it is
yours to decide.
