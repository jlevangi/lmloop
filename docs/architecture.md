# Modules

Python 3.11+, standard library only, no build step. The one exception is the
dashboard's OIDC, which needs PyJWT and requests — imported in `web/auth.py` and
nowhere else, so a missing one disables authentication rather than breaking the
loop.

## The loop

| Module | Responsibility |
|---|---|
| `lmloop.py` | CLI. Subcommands, config overrides, and the narrow-terminal output for `status`/`list`. |
| `loop.py` | The run lifecycle: worktree, iterate, gate, check, commit, stop. Owns the model-role decision. |
| `policy.py` | Pure stop/budget/retry policy, extracted from `loop.Run`: no filesystem, no `self` — a function of its arguments, so it is testable without a worktree or an hour of wall clock. |
| `pi_runner.py` | Runs one iteration of pi and reduces its event stream to an outcome. Supervises the timeout, stall and compaction clocks. |
| `prompts.py` | Builds the iteration prompt. Every section here exists because an agent wasted tool calls re-deriving it. |
| `rundir.py` | Everything a run leaves behind, plus the plan and handoff accessors. Delegates its reading half to `runrecord.py`. |
| `runrecord.py` | The run-record contract shared by the runner and the WebUI: canonical readers for liveness, plan progress, control sentinels, and `run:start`-recorded worktree/branch/owner, plus the `schema_version` marker. Takes a bare run directory path, not a `RunDir`, so it works the same for a live worktree or an archived copy with none. |
| `gitops.py` | Every git invocation. **No reset, no clean, no worktree removal** — grep it. |
| `checks.py` | "Did the edit land intact", on the files git says changed, whatever the project configured. |
| `models.py` | Local-provider preflight and context measurement, plus the per-agent catalogue cache for models it cannot measure. Which provider is "local" is a setting, not a literal. |
| `browser.py` | Whether omp's browser tool can attach to the CDP endpoint it was given. Redacts before it reports. |
| `harness.py` | What lmloop needs from an agent: an argv, what its events mean, and the capabilities the rest of the system used to hardcode by name — default tool allowlist, browser tool, model-listing argv, environment namespace. One small adapter per agent. |
| `config.py` | Defaults → global TOML → the repo's `.lmloop.toml`. Validates both, and resolves secrets a config points at rather than holds. |
| `tools/fake-agent` | An agent that needs no model: same argv shape and event stream as pi, scripted by `.fake-agent.json`. |
| `tools/smoke` | The whole loop against it, in about five seconds, asserting on the commit and the run directory. |
| `doctor.py` | `lmloop doctor`: git, config, agent, model, server, storage, gate and notification. Returns rather than prints, and never raises — a broken environment is when a diagnostic must not add a traceback of its own. |
| `env.py` | What the agent and the gate see of the host environment. An allowlist by default, plus a credential-name filter a blunt prefix rule cannot get past. Pure functions of their arguments; no `os.environ`. |
| `display.py` | The status line and its width arithmetic. Load-bearing; see design.md. |

## The dashboard

| Module | Responsibility |
|---|---|
| `web/server.py` | stdlib `ThreadingHTTPServer`, routing, static, the API. |
| `web/runs.py` | Finds and reads runs across projects. Reads `status.json` rather than replaying history. |
| `web/auth.py` | Who may drive the dashboard: `none` (loopback), `proxy` (identity from a trusted ingress), `oidc` (any issuer). Each mode answers `session_for`; `trusted` is what the network-bind refusal asks. The only place a third-party import appears, and only `oidc` needs it. |
| `web/static/` | Vanilla JS and CSS, no build step. Hash-routed views, keyed row patching. |

Two properties keep it small:

* **Runs are controlled by files.** Pausing is `touch PAUSE`. The dashboard
  never owns a run's lifecycle — it can crash, restart, or be replaced mid-run
  and every control still works.
* **The present is a file.** `status.json` is rewritten every two seconds, so
  there is nothing to reconstruct.

## The shape of an iteration

```
preflight (is the model loadable?)
  └─ build prompt: objective, git state, environment, file tree,
     gate result, defects, plan, handoff
      └─ run pi, supervised: timeout / stall / compaction clocks
          └─ gate (project's own, optional)
              └─ checks (built-in, always)
                  └─ handoff: written by the agent, harvested, or synthesised
                      └─ commit everything, labelled with the outcome
                          └─ persist run state and stop? (turn ceiling, cumulative wall clock, healthy no-diff streak)
```

The order matters in one place: checks run *after* the gate so both results are
available to the same commit and the same next prompt.

## Adding a harness

The loop needs two things from an agent and nothing else: a command that runs
one iteration, and a JSON event stream it can read. `harness.py` is where that
lives — an adapter answers what to exec, which lines are worth parsing, and what
each event means, then everything downstream speaks one normalised vocabulary
and knows nothing about which agent produced it.

`pi`, `omp` and `opencode` are implemented, each written against captured output
rather than documentation. Two of those names collide and it is worth being
precise about which is which: the npm package **oh-my-pi** is a pi *extension*
that pi auto-discovers, so it needs no adapter and arrives through `PiHarness`
unchanged. **`omp`** is [can1357/oh-my-pi](https://github.com/can1357/oh-my-pi),
a fork of pi with its own binary — close enough to inherit most of the pi
adapter, different enough to need one.

What `OmpHarness` overrides is a useful list of what an adapter can be wrong
about: an argv (there is no `--session-id`, and print mode is opt-in), an event
name (`auto_compaction_start`, where pi says `compaction_start` — a prefix
apart, so neither matches the other), and how a tool call names its file (omp's
editor takes a patch script with the path in a `[path#TAG]` header, not a `path`
argument).

An adapter also answers two questions asked outside the stream: which `--tools`
names the agent will accept, because omp exits 1 on one it does not know rather
than ignoring it, and which byte marker finds a compaction summary in a raw
iteration log.

An agent that does not compact simply returns no summary, and the loop falls
back to synthesising a handoff from git — worse, but never wrong.

## Adding a check

`checks.py` is for damage an *edit* does, in any project: a file that stops
parsing, a conflict marker, a block pasted twice. It must stay generic and
quiet — a noisy check is one that gets ignored, and style is the agent's
business.

Anything that encodes what a particular repository considers correct belongs in
that repository, behind its own `[gate] command` in `.lmloop.toml`. Do not add
project-shaped rules here.
