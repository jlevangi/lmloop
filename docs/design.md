# Why lmloop is shaped like this

Every decision below was forced by one fact: **an iteration costs an hour, not
ninety seconds.** Loops built for fast cloud agents can afford to throw away a
bad iteration and try again. At an hour, that trade inverts, and almost
everything else follows.

## The four invariants

### 1. Nothing is ever discarded

There is no `git reset --hard` and no `git clean` anywhere in this codebase.
Grep `gitops.py` for "reset" and you should find only its docstring saying so.
No worktree is ever removed automatically, including one that produced nothing —
a run that failed still has to be diagnosable, and deleting its worktree takes
the only record of *why* with it.

Every iteration that leaves a diff is committed, labelled with what actually
happened. Unwanted work is one `git revert` away at a time of your choosing
rather than the loop's.

The origin: a predecessor discarded 87 minutes of correct, coherent edits
because the agent's final message was not parseable JSON. The code was fine. The
envelope was not.

### 2. Git is the only witness

Not iteration counts, not the agent's own summary, not tool-call counts.

* A cloud routing combo once reported twelve successful iterations across 479
  tool calls and **zero writes**, naming files it had never created.
* The write-tool counter under-counts by design: an agent that appends with a
  bash heredoc changes the tree without touching an edit tool. Observed live.

`git diff` against the run's base commit is the only honest progress signal, and
it is what the stop conditions read.

### 3. The handoff is a file, not a parsed message

The agent writes `handoff.md` with its own write tool. There is no envelope to
fail to parse. Never reintroduce a parse-the-final-message contract.

If the file is missing the loop synthesises one and marks the iteration
degraded — never discarded. If the iteration *overflowed its context*, the loop
harvests the summary pi wrote on the way out and carries that instead: an agent
that ran out of room did write a handoff, just into the event stream rather than
to disk. And a barren iteration never overwrites a good handoff with its own
silence.

### 4. The status line must fit the terminal

This is correctness, not tidiness. An overlong line wraps; after wrapping, `\r`
returns only to the start of the last visual line, the clear leaves the remnant,
and every refresh scrolls a new line. A 61-column terminal turned one model load
into ~30 lines of spam. `display.compose()` drops segments by priority to
prevent it, and every width calculation measures around colour escapes.

## The two files that carry a run

Long-horizon work needs somewhere to keep what it knows. A fresh session every
iteration keeps context bounded, but it also means the agent wakes up knowing
nothing.

| File | Holds | Written by |
|---|---|---|
| `plan.md` | what is *left* — the objective decomposed into steps | the agent, maintained across iterations |
| `handoff.md` | what *happened* — findings, decisions, where to start | the agent, or synthesised |

Both are files for the same reason: nothing here parses a model's prose, and a
file survives a context overflow that a chain of reasoning does not.

`plan.md` is the newer of the two and the one that made big objectives work at
all. Before it, every iteration re-derived "what do I do now" from the whole
objective, which meant surveying the whole codebase, which meant overflowing
before writing anything.

## What the loop does *not* do

* **It does not choose tools, prompts, skills or models for the agent.** It
  drives `pi` and nothing else; those belong to the agent.
* **It does not block commits on a failing gate** by default. The gate result is
  recorded in the commit message and the next prompt. Work is never held hostage
  to a check.
* **It does not judge correctness.** `checks.py` asks only "did the edit land
  intact". Whether the program is *right* is what a project's own gate is for.
