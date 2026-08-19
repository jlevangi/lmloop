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
| `pi_runner.py` | Runs one iteration of pi and reduces its event stream to an outcome. Supervises the timeout, stall and compaction clocks. |
| `prompts.py` | Builds the iteration prompt. Every section here exists because an agent wasted tool calls re-deriving it. |
| `rundir.py` | Everything a run leaves behind, plus the plan and handoff accessors. |
| `gitops.py` | Every git invocation. **No reset, no clean, no worktree removal** — grep it. |
| `checks.py` | "Did the edit land intact", on the files git says changed, whatever the project configured. |
| `models.py` | llama-swap preflight and context measurement. |
| `harness.py` | What lmloop needs from an agent: an argv, and what its events mean. One small adapter per agent. |
| `config.py` | Defaults → global TOML → the repo's `.lmloop.toml`. |
| `display.py` | The status line and its width arithmetic. Load-bearing; see design.md. |

## The dashboard

| Module | Responsibility |
|---|---|
| `web/server.py` | stdlib `ThreadingHTTPServer`, routing, static, the API. |
| `web/runs.py` | Finds and reads runs across projects. Reads `status.json` rather than replaying history. |
| `web/auth.py` | OIDC. The only place a third-party import appears. |
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
                          └─ stop? (iterations, wall clock, no-diff streak)
```

The order matters in one place: checks run *after* the gate so both results are
available to the same commit and the same next prompt.

## Adding a harness

The loop needs two things from an agent and nothing else: a command that runs
one iteration, and a JSON event stream it can read. `harness.py` is where that
lives — an adapter answers what to exec, which lines are worth parsing, and what
each event means, then everything downstream speaks one normalised vocabulary
and knows nothing about which agent produced it.

`pi` and `opencode` are both implemented, each written against captured output
rather than documentation. `oh-my-pi` needs no adapter: it is a pi *extension*
that pi auto-discovers, so it arrives through the pi adapter unchanged.

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
