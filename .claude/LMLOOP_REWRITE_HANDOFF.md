# lmloop — state after the portability rewrite

Date: 2026-08-26. Supersedes the pre-rewrite audit that lived at this path.

## Where things stand

`lm-ka5` (portability rewrite) and all nine children are **closed**, and so is
`lm-5j3`, which brought the dashboard up to what the rewrite left it saying.
The suite went from 60 tests to 620. `tools/smoke` runs the entire loop — worktree,
prompt, subprocess, event reduction, checks, gate, commit, finalisation — in
about five seconds with no GPU, no API key and no network.

Verified rather than asserted, at close:

- all six real archived runs predating `schema_version` still read correctly
  (plan progress, events, resolved branches);
- pi, omp and opencode each drove a real iteration against the live llama-swap
  and committed correct code, switching by `[agent] harness` + `model` alone;
- with `local_providers = []` and no OIDC on a bare `HOME`, `lmloop doctor`
  is clean and the dashboard serves on loopback.

## How to work on this

```bash
python3 -m compileall -q . web/
python3 -m unittest          # 620 tests, ~13s
tools/smoke                  # the whole loop, no model, ~5s
lmloop doctor                # in any repo you are about to run against
```

`tools/smoke` first, always. It found an unbounded-run bug on its first
execution that no amount of reading had.

## The method that produced this, and why it is not optional

Every move was made behind characterisation tests written **first**, and every
one was **mutation-verified** — break the guard on purpose, confirm exactly the
right test fails. That is not ceremony. It found, in this codebase:

- eight guards with no coverage at all, including the only untrusted path
  component in the system (`create_project`'s name);
- six real bugs, each already shipped: unbounded runs when the agent writes no
  plan, a progress witness blind to new files, a hung tool call with no clock,
  a six-hour wait for a server cloud runs never touch, omp reporting no context
  window, and a test that passed by winning a race.

Three times a test disagreed with a plan and the test was right. The bead for
`lm-j44` proposed matching the run id; checking a live loop first showed
`lmloop run "objective"` carries no run id at all. The bead for `lm-lt6` blamed
pi for a filter that is not in pi. `lm-oit` asked for a host change the hardware
cannot make.

**Check the claim before implementing the fix it implies.**

## Shape

| where | what |
|---|---|
| `loop.py` | the run lifecycle; orchestration only — the decisions moved out |
| `policy.py` | pure stop/budget/retry/pressure arithmetic, no `self`, no I/O |
| `runrecord.py` | the run-record contract shared by runner and WebUI |
| `harness.py` | one adapter per agent; capabilities, not name checks |
| `models.py` | local-provider preflight and measurement; `local_providers` is config |
| `env.py` | what an agent and a gate see of the environment — an allowlist |
| `config.py` | layered TOML, validated with actionable errors, secrets by reference |
| `doctor.py` | everything that must be true before a run can work |
| `attach.py` | the foreground screen for a detached run |
| `web/server.py` | HTTP only: routing, auth, marshalling |
| `web/service.py` | what the dashboard does; returns `(status, payload)` |
| `web/workspace.py` | the only code that removes a worktree or leaves the machine |
| `tools/fake-agent` | an agent that needs no model, scripted by `.fake-agent.json` |

## Invariants, and how they are enforced now

1. **Nothing is discarded.** No reset, no clean, no automatic worktree removal.
   Checked project-wide: a test parses every `git`/`gh` argv with `ast`, so a
   comment mentioning `reset` costs nothing and a real one cannot hide behind
   it. `web/workspace.py` is the single allowed home for operator-initiated
   removal, and its worktree removal is never `--force` — git's refusal is what
   protects an agent's uncommitted work.
2. **Git is the only witness** — and it can now see new files.
3. **Plan and handoff are durable files**, never parsed prose.
4. **The status line fits the terminal.**

## The dashboard, after `lm-5j3`

`lm-5j3` and all five children are **closed**. The suite is 620 tests. Four
things the API had grown and the page never said are now said, all additive to
an existing surface and with the visual language untouched:

| what | where it comes from |
|---|---|
| which agent produced a run | the `run:start` event's `agent`, not the config |
| why a model list is not a catalogue | `/api/models`' `model_source` |
| which iterations ran out of room | the loop's own `context:pressure` events |
| who you are signed in as | `/api/config`'s `auth` and `user` |

**The frontend is static files, so nothing checks it against the API but a test
that reads both.** `test_web_frontend.py` is that test and is the pattern to
follow for anything added to the dashboard: it guards the outcome, state, run
field, `model_source` and auth-mode vocabularies, reading each one from the
source that produces it rather than from a list. It began with `a19f1a2`, where
`tool-timeout` was added to the runner and the dashboard never learned the
word, so a failure rendered as a neutral pip.

Two of the four beads had **stale premises**, and checking before implementing
is what caught both. `lm-5j3.1` had already been shipped three days before it
was written — and named the field `harness`, where the API serves `agent`, so
implementing it literally would have rendered nothing. `lm-5j3.4` described a
boolean the page did not read either. `lm-5j3.3`'s design would have marked
rows from the run-level window, which is wrong: planning and a thrash retry
escalate to different models, so the window is not constant across one run.

`lm-5j3.5` was found by *probing rather than reading*, while checking what
`model_source` really returns: `web/server.py` parsed every agent's catalogue
with pi's column parser, and `omp models` prints a box-drawing table, so the
dashboard offered `9router/(97)` and `llama-swap/(7)` — two models that do not
exist — and reported them as omp's own catalogue. Asking and parsing now both
belong to the adapter (`Harness.catalogue`), with `list_models_argv` left as
the question asked *for a person*.

### Verifying dashboard work

Reading the file proves nothing; a CSS regression has already survived a clean
parse here. Drive it in a real browser:

```bash
lmloop web --port 8099 --host 127.0.0.1 --env /dev/null --roots <scratch>
```

`chrome --headless --dump-dom` **hangs** on this page — the poll loop never
lets virtual time drain. Attach over CDP instead; there is a `ws` module under
`~/.local/lib/node_modules/happy/node_modules/ws`, and `Emulation.
setDeviceMetricsOverride` is how the 390px case gets checked. To reach a state
a scratch run cannot produce, script `tools/fake-agent`: `input_tokens` in
`.fake-agent.json` fills a window without a model, and declaring that window in
a scratch `HOME`'s `~/.pi/agent/models.json` keeps `local_providers` empty so
nothing preflights a model server.

## What is open

`lm-lt6` — `moshi-hook`, a third-party approval daemon hooked into pi, blocked
`git` during a real unattended run (`[SECURITY] Blocked command: git (max
mode)`). Not lmloop's to fix; the user's security tooling. `lmloop doctor` now
names what is loaded into the agent so it is at least visible. Left open by the
user's choice.

`lm-z90` — a different project.
