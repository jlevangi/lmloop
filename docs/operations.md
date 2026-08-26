# Running lmloop

## Starting and steering a run

```bash
cd ~/git/some-project
lmloop run "a broad objective"           # decomposed by the agent, not by you
lmloop run "..." --model llama-swap/local-wide --thinking low
lmloop run "..." --detach                # background; prints the run id
lmloop resume <run-id> --iterations 12   # extend its saved ceiling by 12 turns
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

## Choosing the agent

lmloop drives three, selected by name and never inferred:

| `[agent] harness` | binary | what it is |
|---|---|---|
| `pi` (default) | `pi` | pi, plus every extension pi discovers — including the npm package called **oh-my-pi**, which is an extension and needs no adapter |
| `omp` | `omp` | **[can1357/oh-my-pi](https://github.com/can1357/oh-my-pi)**, a fork of pi with its own binary and its own browser, task and LSP tools |
| `opencode` | `opencode` | opencode, via `run --format json` |

Those first two are different projects with confusingly similar names. The npm
`oh-my-pi` is an orchestration layer that plugs into pi; `omp` is a separate
agent. Selecting `omp` when you meant the extension gets you a binary you may
not have installed, and selecting `pi` when you meant `omp` silently gets you no
browser at all.

Set it in the config, or per run:

```bash
lmloop run "..." --agent omp
lmloop resume --agent pi          # roll back mid-run; see below
```

### Installing omp beside pi

They do not collide. omp's only declared binary is `omp`, its state lives under
`~/.omp` rather than `~/.pi`, and nothing in it writes a `pi`. Install it
without touching the pi you already have:

```bash
# prebuilt binary, pinned; PI_INSTALL_DIR defaults to ~/.local/bin
PI_INSTALL_DIR=~/.local/bin sh -c 'curl -fsSL https://omp.sh/install | sh -s -- --ref v17.4.0'

omp --version            # → omp/17.4.0
ls -l "$(command -v pi)" # unchanged
```

The npm route (`bun install -g @oh-my-pi/pi-coding-agent`) works too, but the
published shebang is `#!/usr/bin/env bun` and `engines` names only bun, so an
`npm install -g` on a box without bun gives you a shim that cannot start. The
prebuilt binary has no runtime dependency.

**Everything lmloop's omp adapter knows was captured from v17.4.0.** Upgrading
is not forbidden, but the event names and flags below were verified against that
version and nothing else; re-run the test suite after you do.

### The tool allowlist

omp checks `--tools` against its own built-ins and exits 1 before emitting a
single event — `Unknown tools in --tools: replace, ls`. pi's default allowlist
names both, so the two are not interchangeable. lmloop settles this for you:
selecting `omp` with the allowlist still at its shipped default swaps in omp's
own, and any other list is validated when the config is read rather than an hour
into a run.

| what for | `[agent] tools` |
|---|---|
| general work (the default under `omp`) | `read,write,edit,bash,grep,glob` |
| work with a user interface in it | `read,edit,grep,glob,bash,browser` |

The second list has no `write`, deliberately: it is for changing an interface
that already exists. Add `write` when the task genuinely creates files — omp's
`edit` is a patch language that refuses to touch a file it has not just read and
cannot create one at all.

### Rolling back to pi

Nothing about a run is omp-shaped. The worktree, the branch, the plan, the
handoff and every commit are the same files whichever agent produced them, so
rolling back is a flag and costs nothing already done:

```bash
lmloop resume <run-id> --agent pi --iterations 3
```

Set `harness = "pi"` in `.lmloop.toml` to make it stick. There is nothing to
uninstall and nothing to migrate.

## Pointing omp at a browser

omp's `browser` tool drives a real Chromium tab over the DevTools Protocol. It
resolves where to attach in this order, and the first one that answers wins:

1. `app.cdp_url` on the tool call itself
2. `app.path` — a browser it launches
3. `app.relay: true`, or the `browser.relay` setting: the operator's own Chrome,
   through `omp browser-relay` and its extension
4. the `browser.cdpUrl` setting — `omp config set browser.cdpUrl <url>`
5. a headless Chromium of its own

Two constraints decide whether an existing browser is reachable at all, and both
were read out of the binary and confirmed against a live endpoint:

* **It must be an HTTP *discovery* endpoint.** `http://host:9222`, the thing
  that answers `/json/version`. A `ws://` or `wss://` URL — which is what a
  hosted browser usually advertises — is rejected by name: *"must be the HTTP
  CDP discovery endpoint (for example http://127.0.0.1:9222), not a ws://
  browser websocket URL."*
* **A credential in the query string does not survive.** omp polls
  `${cdpUrl}/json/version` by concatenation and then hands the URL to
  puppeteer's `browserURL`, which resolves the path against the origin. Neither
  form carries `?token=...`. The symptom is a five-second attach timeout and
  nothing else said.

So a loopback endpoint that answers `/json/version` unauthenticated attaches
natively today, and a token-authenticated one needs a local shim that injects
the credential and rewrites the websocket URL it advertises. lmloop does not
ship that shim; it tells you which case you are in.

### The preflight

Set the endpoint and lmloop checks it once, when the run starts, before an
iteration can waste an hour on it:

```toml
[agent]
harness         = "omp"
tools           = "read,edit,grep,glob,bash,browser"
browser_cdp_url = "http://127.0.0.1:9222"
```

```
  browser: Chrome/150.0.7871.124 at http://127.0.0.1:9222
  browser: ... requires authentication (HTTP 401); the query credential does not
           survive omp's attach -- a loopback shim that injects it is required
           (the agent will run without it)
```

It is never fatal — an iteration with no browser still reads, edits, gates and
commits — and it never opens a tab, because a preflight that drives the browser
is one that can disturb whatever is already using it. Every query value is
redacted before the line is printed or written to the event log: a CDP endpoint
is credentials, and anything that can reach it can read every page the browser
has open. Leaving `browser_cdp_url` empty skips the check and leaves the tool to
omp's own configuration.

## Pointing omp at llama-swap

omp reads providers from `~/.omp/agent/models.yml` — not from `--config`, which
carries settings only, and not from pi's `models.json`. Routing llama-swap to it
is one block appended, touching nothing already there:

```yaml
providers:
  llama-swap:
    baseUrl: http://127.0.0.1:8080/v1
    api: openai-completions
    auth: none
    discovery:
      type: openai-models-list
      timeoutMs: 15000
```

Name the provider `llama-swap` exactly. lmloop's own preflight and window
arithmetic key off that prefix — `models.declared_window` reads the measured
`--ctx-size` cache for it, and `models.preflight` asks llama-swap what is loaded
without naming a model and so without causing a swap. Call it anything else and
a local model silently starts being treated as an unmeasurable cloud one.

Then:

```toml
[agent]
harness = "omp"
model   = "llama-swap/<model>"
```

`PI_CODING_AGENT_DIR` relocates that whole directory if you want a config
isolated from your interactive omp.

Discovery finds the models but not their windows — `openai-models-list` returns
ids and nothing else, so omp guesses. Measured on one box, it guessed 262144 for
a `Qwen3.8-27B` actually loaded with `--ctx-size 131072`, and omp compacts
against its own number, not lmloop's. Declare them, matching what
`lmloop models` reports:

```yaml
    models:
      - id: Qwen3.8-27B
        contextWindow: 106496
        maxTokens: 24576
```

`models` must be an **array**; an object fails validation with
`providers.llama-swap.models: must be an array (was an object)` and — worth
knowing — that error disables *every* custom provider, not just the broken one,
so a typo here takes 9router down with it. omp says so on stderr and then
reports "No models available".

Cloud models under omp are no longer a gap: lmloop asks `omp models --json` for
its catalogue rather than reading pi's `models.json`, which is what it used to
do. See `Harness.declared_windows`.

## Pointing opencode at llama-swap

opencode keeps providers in `~/.config/opencode/opencode.json`. Same shape of
block, and the same reason to declare the windows rather than let it guess:

```json
"provider": {
  "llama-swap": {
    "npm": "@ai-sdk/openai-compatible",
    "name": "llama-swap (direct)",
    "options": { "baseURL": "http://127.0.0.1:8080/v1" },
    "models": {
      "Qwen3.8-27B": { "name": "Qwen3.8-27B",
                       "limit": { "context": 106496, "output": 24576 } }
    }
  }
}
```

```toml
[agent]
harness = "opencode"
model   = "llama-swap/<model>"
```

opencode takes no `[agent] tools` — it has no allowlist flag — so leave it out.

**Go direct, not through a router.** An opencode run pointed at the same
llama-swap *through* 9router produced no output at all: nine minutes, a
zero-byte stream, nothing to diagnose. Pointed straight at llama-swap the same
objective committed in 1m02s. The router is for when you actually want a cloud
model.

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

Add `--agent omp` to run the same thing through omp, against any model
`omp models` already lists. The adapter's own tests need no agent at all: they
run against event lines captured from a real `omp -p --mode json`, so
`python3 -m unittest test_lmloop` proves the parsing without a model, a network
or a browser.

For adapter work — a new event name, a tool argument that moved — neither a
cloud model nor llama-swap is the right instrument, because neither lets you
decide what the agent will do next. Both agents take `PI_CODING_AGENT_DIR`,
which relocates their whole config directory, so point one at a scratch
directory holding nothing but a provider aimed at a local OpenAI-compatible
server you wrote:

```bash
export PI_CODING_AGENT_DIR=/tmp/agentcfg      # omp: models.yml, pi: models.json
lmloop run "..." --agent omp --model stub/stub-tiny --max-iterations 1
```

Roughly ninety lines of `http.server` returning a scripted SSE stream is enough
to make either agent call any tool you like, in any order, and to make it fail
in whatever way you are trying to handle. That is how everything the omp adapter
claims was established: a real agent binary, a real event stream, a model that
does exactly as it is told, and llama-swap never touched.

For display work, force a narrow terminal — this is the phone case and how the
wrapping bug was found:

```bash
script -qec "stty cols 32 rows 20; lmloop run '...' --max-iterations 1" out.log
sed 's/\x1b\[[0-9;]*[a-zA-Z]//g' out.log | tr '\r' '\n' | tail -20
```

## A first omp run, deliberately small

Before an omp run is worth hours, prove the parts that are new in one iteration.
The order matters: each step below fails cheaply and locally, and only the last
one costs model time.

```bash
# 1. the binary is the one you think it is, and pi is untouched
omp --version                    # → omp/17.4.0
ls -l "$(command -v pi)"

# 2. the adapter parses what omp actually emits
cd <lmloop checkout> && python3 -m compileall -q . web/ && python3 -m unittest test_lmloop

# 3. llama-swap is routed, and nothing is loading right now
grep -A6 '^  llama-swap:' ~/.omp/agent/models.yml
curl -s http://127.0.0.1:8080/running | python3 -m json.tool

# 4. one iteration, on the repository you mean, watched
lmloop run "<one small objective>" --agent omp --model llama-swap/<model> --max-iterations 1
```

Step 3 is `GET /running`, which is free and never triggers a swap. Do not
substitute `GET /upstream/<model>/props`: that endpoint *causes* the swap it
looks like it is observing, and has stalled a live run.

Read the result the way any run is read — `git log` on the branch, then
`.lmloop/runs/<id>/lmloop.log` for the `run:start` line, which now names the
agent that produced it. What you are looking for in the first one is narrow:
`agent` says `omp`, the iteration reports tool calls rather than
`no-action`, and `files` in the commit matches what the diff says. If the
allowlist included `browser`, the `browser:` line at the top of the run has
already told you whether the tab was ever reachable.

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
