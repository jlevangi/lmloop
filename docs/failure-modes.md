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

**Evidence.** one-project, local-fast: ten compactions across two iterations,
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

**Fixes.** Outcome `truncated`; per-model output overrides in the pi catalogue
extension (local-wide 90112+8192 → 73728+24576, local-fast 57344+8192 → 49152+16384); and
`[agent] thinking` to lower deliberation.

## The worktree has no environment

**Shape.** `git worktree add` materialises **tracked files only**. Every
ecosystem keeps its dependencies untracked, so the agent gets the source and
nothing that runs it.

**Evidence.** one-project: 74 minutes and 24 tool calls enumerating every
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

**Evidence.** one-project's gate was `compileall -q backend` while the run was
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

### What happens after a thrash

Two things, automatically, and neither of them edits the plan.

**The retry goes to a wider model.** Thrashing is the window losing to the
codebase, so retrying the same step on the same model retries what just failed
for a reason that has not changed. The next iteration runs on whichever model
the project already names that measures widest — on one-project, 90112 tokens of
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
