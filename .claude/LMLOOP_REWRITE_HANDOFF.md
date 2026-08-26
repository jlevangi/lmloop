# lmloop — state after the portability rewrite

Date: 2026-08-26. Supersedes the pre-rewrite audit that lived at this path.

## Where things stand

`lm-ka5` (portability rewrite) and all nine children are **closed**. The suite
went from 60 tests to 588. `tools/smoke` runs the entire loop — worktree,
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
python3 -m unittest          # 588 tests, ~13s
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

## What is open

`lm-5j3` — bring the dashboard up to the rewritten API. Four children, all
additive to existing surfaces; the style is wanted as it is. One bug of this
class is already fixed (`a19f1a2`): `tool-timeout` was added to the runner and
the dashboard never learned the word, so a failure rendered as a neutral pip.
`test_web_frontend.py` now guards the outcome and state vocabularies — the
frontend is static files, so nothing checks it against the API but a test that
reads both.

`lm-lt6` — `moshi-hook`, a third-party approval daemon hooked into pi, blocked
`git` during a real unattended run (`[SECURITY] Blocked command: git (max
mode)`). Not lmloop's to fix; the user's security tooling. `lmloop doctor` now
names what is loaded into the agent so it is at least visible. Left open by the
user's choice.

`lm-z90` — a different project.
