# How runs fail, and how each one was found

Every failure here was observed on real hardware against a real repository. They
are written down because each cost hours to diagnose and every one of them
looked, from the outside, exactly like "the model isn't good enough".

The recurring lesson: **an outcome of `ok` was the most expensive bug in the
system.** Four distinct failures all reported success, so nobody reached for the
fix while the log said the run was fine.

## Compaction thrash

**Shape.** The agent reads a dozen files, overflows its context, pi compacts
silently, the model distrusts the summary and re-reads the same dozen files.
Repeat until the iteration dies.

**Evidence.** A frontend project, on the 8B workhorse: ten compactions across two iterations,
every one `reason: "overflow"`; 137 tool calls, all `read`/`ls`; zero writes.
The compaction summary grew 4.9K → 8.1K → 10.6K → 15.8K bytes, so the usable
window *shrank* each cycle and the loop tightened rather than converging.

**Why it looked like something else.** The model was not confused. Its final
compaction summary contained a correct, complete plan, including a
self-correction about whether `create_app()` calls `db.create_all()`. It was
never short of understanding — only of room.

**Fixes.** `max_compactions` ends the iteration as `thrashing`; the compaction
summary is harvested as the next handoff; the file tree is precomputed with line
counts; and `plan.md` means an iteration only needs the files for *one step*.

## Output-budget exhaustion

**Shape.** The model deliberates until its output cap is reached, the message
ends mid-sentence, and the tool call it was building never arrives.

**Evidence.** local-wide: 19 minutes, **zero tool calls**, exactly 8192 output
tokens. local-fast on a narrower objective: 45,457 characters of "Actually,
let me reconsider…" then `stopReason: length` at the same 8192.

**Why it hid.** `stopReason: length` is not an error, so the loop called it
`ok`. Nobody raises an output budget while the log says success.

**Fixes.** Outcome `truncated`; per-model output overrides (local-fast 57344+8192 →
49152+16384; local-wide was 90112+8192 → 73728+24576 and is now 172032+24576 after its
`--ctx-size` was raised); and `[agent] thinking` to lower deliberation.

Those overrides now live in `~/.config/lmloop/model-budgets.json`, read by both
the pi extension and `models.py`. They were written down separately in each and
had drifted -- see *One place to change it* in `docs/models.md`.

## The worktree has no environment

**Shape.** `git worktree add` materialises **tracked files only**. Every
ecosystem keeps its dependencies untracked, so the agent gets the source and
nothing that runs it.

**Evidence.** A Flask project: 74 minutes and 24 tool calls enumerating every
`python3` on the box looking for one that could import Flask, then a stall while
building its own virtualenv with `uv`. Flask was in the repo's `.venv`, one
symlink away. The same cause had already produced `rc=127` from the gate on
iteration 1.

**Fixes.** `[worktree] link` symlinks untracked dependencies in, and the prompt
*names the interpreter* — an agent that does not know one exists goes looking
for it, which is half the cost.

## The gate cannot see the work

**Shape.** A project's gate checks the dimensions someone anticipated. A run
working outside them commits broken output as a success.

**Evidence.** One project's gate was `compileall -q backend` while the run was
doing pure CSS. Six iterations committed a duplicated fragment — an orphaned
declaration and an extra `}` closing a media query early — written by the
iteration with ten context overflows.

**Fixes.** `checks.py` runs on every iteration regardless of configuration and
asks only "did the edit land intact": parse failures, conflict markers, and runs
of lines pasted twice. When it finds something, repair becomes the iteration's
job and the plan waits.

**Still open.** Structural checks cannot catch *semantic* breakage. The same run
later moved rules into `components/_tables.css` and never added the `@import`,
so 47 selectors vanished from the rendered page while every file parsed
perfectly. Only comparing against the stylesheets the page actually loads found
it — and a first check that searched the css *directory* passed, because the
orphaned legacy files were still sitting there.

## The objective is too big to hold

**Shape.** Every iteration re-derives "what should I do now" from the whole
objective, so it surveys the whole codebase, so it overflows before writing.

**Evidence.** "Add a test suite … cover the main /api routes" against a 57344
window: 177 tool calls across six iterations, zero writes, on two different
models. Narrowing the objective by hand fixed it instantly — which was the wrong
fix, because decomposition is the loop's job.

**Fix.** `plan.md`. The agent writes it from the precomputed file list *without
reading files*, then does the first unchecked step and nothing else. The same
broad objective then produced 124 passing tests over 14 iterations.

## Choosing omp and getting pi's vocabulary

Three failures share one cause: omp is close enough to pi that a setting looks
portable, and different enough that it is not. All three were reproduced against
omp v17.4.0 rather than reasoned about.

**The allowlist that stops the run before it starts.** pi's default `tools`
names `replace` and `ls`. pi ignores what it does not recognise; omp checks the
list and exits 1 — `CliUsageError: Unknown tools in --tools: replace, ls` —
before it emits a single event. Under `--mode json` that is one of the very few
ways omp does exit non-zero, and the loop would have seen an iteration with no
message end and reported `agent-error` every time. `config.resolve_tools` now
settles this while the config is being read: the shipped default is swapped for
omp's, and anything else is validated with the offending names quoted.

**The compaction summary that silently is not there.** pi emits
`compaction_start` / `compaction_end`; omp emits `auto_compaction_start` /
`auto_compaction_end`. The names are a prefix apart, so a marker for either
matches neither. An omp iteration that overflowed would have looked exactly like
one that never compacted: the thrash counter would not tick, and
`last_compaction_summary` would find nothing and fall back to synthesising a
handoff from a diff that, for the iterations this happens to, is empty. Both the
byte marker and the event name now come from the adapter.

**The editor with no path.** omp's `edit` takes one string — a line-anchored
patch script whose file is named in a `[path#TAG]` section header — where pi's
takes a `path`. Nothing crashes: `files_touched` just comes back empty for every
edit an omp run makes, and the commit trailer that lists what changed lists
nothing while the diff says otherwise. Git is still the witness, so no work is
lost; what is lost is the ability to read the record. The adapter parses the
header, and returns nothing rather than a guess when there is not one.

## The browser that was never reachable

omp is the first agent here with a browser of its own, and an unreachable one
fails as a slow, expensive, plausible-looking dead end: the agent opens a tab,
waits five seconds, gets nothing, tries something else, and the run log reads as
a model that could not do the task.

Two properties of omp's attach decide it, and neither is obvious from a URL that
looks fine. It takes an HTTP CDP *discovery* endpoint and rejects `ws://` and
`wss://` by name — which is exactly the form a hosted browser advertises. And it
reaches that endpoint as `${cdpUrl}/json/version`, then hands the URL to
puppeteer's `browserURL`, so a `?token=` credential is dropped both times.
Against a token-authenticated endpoint the result is a five-second timeout with
nothing said, and no amount of the agent retrying will change it.

`browser.py` answers this once, when the run starts, and never opens a tab: a
preflight that drives the browser is one that can disturb whatever else is using
it. It is not fatal — the iteration still reads, edits, gates and commits. Every
query value is redacted before the answer reaches the terminal or the event log,
because a CDP endpoint is credentials: anything that can reach it can read every
page that browser has open.

## Reporting failures honestly

| Outcome | Means |
|---|---|
| `ok` | finished, called at least one tool, did not hit a cap |
| `thrashing` | overflowed `max_compactions` times with no writes |
| `truncated` | ran out of output budget mid-message |
| `no-action` | ended cleanly having called no tool at all |
| `stalled` | silent for `stall_seconds` after the model was demonstrably alive |
| `timeout` | exceeded `timeout_seconds` |
| `interrupted` | STOP-NOW sentinel or signal, mid-iteration |
| `agent-error` | pi reported an error, or produced no assistant message |

Collapsing these to pass/fail throws away the diagnosis, and they imply
different fixes: `thrashing` wants a smaller step, `truncated` wants a bigger
output budget or less thinking, `stalled` wants investigating.

### When the server went away, not the model

`agent-error` covers two unrelated things: the agent did something wrong, and
the model server disappeared underneath it. Only the first is worth spending an
iteration on.

A run whose iteration ends in `agent-error`, left no commit, and whose detail
names a transport failure — `Stream ended without finish_reason`, a refused or
reset connection, a timeout, an unresolvable host, a 502/503/504 — is retried on
the same backoff as a failed preflight (1m, 2m, 4m, then give up), reusing the
iteration number. pi retries these itself first, so one that reaches lmloop
means the server was gone for minutes: a restart, a reload, a model swap.

Observed here: fifty minutes of generation ended by a llama-server being
swapped for a faster build mid-stream. That cost one of twelve iterations for a
reason that had nothing to do with the work.

Then observed again, and the list did not cover it. llama-swap was shut down
deliberately, 23 minutes into an iteration, and what pi reported was
`Request timed out.` — which matched none of the phrases above. The loop
recorded a genuine `agent-error` and charged the run an iteration for a machine
that had been switched off. The lesson is not "add one more string": it is that
this list is a guess about another program's wording, and every phrase in it was
added after watching it happen. Expect to add more.

Matching a bare `timed out` is safe despite how broad it reads, and for a
structural reason rather than a lucky one. lmloop's own clocks never produce
`agent-error` — an iteration that outruns `timeout_seconds` is `timeout`, and
one that goes quiet is `stalled`. So a timeout reported from inside an
`agent-error` is the agent timing out on the model, which is the transport by
definition.

The commit check is the guard. If the agent got far enough to change files, the
iteration is worth keeping whatever killed it, and redoing it would mean redoing
work that is already in git.

### What happens after a thrash

Two things, automatically, and neither of them edits the plan.

**The retry goes to a wider model.** Thrashing is the window losing to the
codebase, so retrying the same step on the same model retries what just failed
for a reason that has not changed. The next iteration runs on whichever model
the project already names that measures widest — on one repository, 90112 tokens of
prompt budget against 49152. Only configured models are considered: picking one
the operator never named would be lmloop deciding what their hardware should
load. If nothing configured is wider, or the models are unmeasured, nothing
changes.

**The next prompt names the step that thrashed** and asks the agent to split it
before attempting it again — by *file*, not by concept. Restating one goal as
three sub-goals changes nothing, because each still needs the same files open at
once; the read budget is what overflowed, and only naming fewer files per step
reduces it.

The agent does the splitting. The harness never edits `plan.md` — the plan is
the agent's, and a harness that silently rewrites it is a harness whose state
the agent can no longer trust. The warning retires itself as soon as the step
it names stops being the first unchecked one, whether the agent split it or
finished it.
