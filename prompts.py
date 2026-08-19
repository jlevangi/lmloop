"""The iteration prompt.

A fresh session every iteration bounds context against a hard 65,536-token
window, but it also means the agent wakes up knowing nothing.  One observed
the predecessor iteration spent 43 file reads just working out where it was -- at ~15
tok/s that is a large fraction of an iteration spent re-deriving facts the loop
already has.  So the loop pays that cost once, in Python, and hands the answer
over.

The first version of this prompt precomputed the git log and the diff, and that
was not enough: **both are empty on iteration 1 of a fresh run**, so the warm
prompt was coldest exactly when the agent was.  Nothing in it described the
shape of the repository, and on one-project the agent bought that shape with 18
`ls`/`read` calls, overflowed its 57344-token window, compacted, distrusted the
compaction, and re-read the same twelve files -- six times in 69 minutes,
finishing with 81 tool calls and an untouched worktree.  So the file inventory
is precomputed too, and the instructions below budget orientation instead of
inviting it.

The other lesson from that run is ordering.  "Write the handoff before you
finish" was the last line of the prompt, so it was the last thing attempted, and
on a window this size it was never reached.  What the agent has to be told is
that finishing is not the point: the run is a chain of iterations, and a written
file beats a better plan.

The other job here is the handoff instruction.  the predecessor demands the final
assistant message validate against a JSON schema, so a model that does good work
and then wraps its JSON in prose loses the work.  Asking for a *file* instead
removes the envelope entirely: writing it is a tool call, the result is on disk,
and there is nothing for the loop to fail to parse.
"""

from __future__ import annotations

TEMPLATE = """\
# Objective

{objective}

# Where things stand

You are iteration {number} of at most {max_iterations}, working in a git
worktree on branch `{branch}`. Progress is measured against `{base}`.

Commits so far this run:
{log}

Cumulative diff against the base commit:
{diff}

{environment_section}{tree_section}{gate_section}{defects_section}{plan_section}# Handoff from the previous iteration

{handoff}

# What to do now

{task_line} You are one iteration in a
long chain: the objective is not yours to finish today, and there is no credit
for getting further than the step. The run is measured in commits, so one small
finished step beats three half-finished ones.

**Read at most three files before your first write.** This is a hard budget, not
advice. Your context window is smaller than this codebase: if you fill it with
file contents before writing anything, it overflows, you lose everything you
read, and you start over from a summary -- three times in a row on one observed
iteration, forty file reads, not one line written. Surveying everything you
might need is the single most reliable way to finish an iteration with nothing.

So: read the two or three files the next change actually requires, write the
change, and only then read more. A rough file you improve next iteration is
worth more than a perfect plan you never got to type, because the rough file is
committed and the plan dies with your context. If the objective is bigger than
three files, it is too big to be one step: split it in the plan and do the first
piece.

Rules:

- Do NOT run `git commit`, `git reset`, or `git checkout`. The harness commits
  everything you leave behind, whether or not you finish cleanly.
- Do NOT edit anything under `.lmloop/` except `plan.md` and `handoff.md`.
- The repository inventory above was precomputed for you. Use it instead of
  exploring with `ls`, and use `grep` rather than reading a whole file to find
  one thing in it.
- Write `{handoff_path}` using your write tool. Line 1 must be a single-line
  summary of what you did this iteration -- it becomes the commit subject. After
  that, in whatever form is useful: what you changed, what you learned about
  this codebase, and what the next iteration should do first. Be specific about
  file paths; the next iteration starts with no memory of this one and only this
  file to go on. Do not save this for the end -- write it as soon as you have
  done one useful thing, and update it if you do more.
"""

PLAN_TEMPLATE = """\
# The plan

{body}
"""

NO_PLAN = """\
There is no plan yet.  Writing one is the first thing you do this iteration.

The objective above is too large to hold in your context all at once -- trying
to is how previous runs failed, surveying the whole codebase until the window
overflowed and nothing got written.  So break it into steps small enough that
each one touches one or two files and finishes inside a single iteration.

Build the plan from the file list above.  Do NOT read files to plan; the list
tells you what exists, and that is all planning needs.  Write it to `{path}` as
markdown checkboxes, most useful first:

    - [ ] one small step
    - [ ] the next one
    - [ ] ...

Then do the FIRST step, check it off, and stop.  You are not expected to finish
the objective this iteration, or in the next five.  There will be many more.
"""

HAVE_PLAN = """\
This is the plan for the objective, as you left it.  {progress}

```
{plan}
```

Do the FIRST unchecked step and nothing else.  When it is done, edit `{path}` to
check it off.

Two things are always allowed and often right: splitting a step you have
discovered is too big into smaller unchecked steps, and adding steps you have
realised are necessary.  Do not rewrite the whole plan, do not re-order what is
already done, and do not start a second step because the first was quick -- the
next iteration will pick it up, and a small finished step beats two half-done
ones.
"""

ENVIRONMENT_TEMPLATE = """\
# The environment

{body}
"""

INTERPRETER_LINE = """\
Run Python with `{interpreter}`, which has this project's dependencies
installed.  The bare `python3` on PATH does not have them, so anything you run
with it will fail on imports that are in fact available.
"""

LINKED_LINE = """\
`{names}` {verb} linked in from the repository, so the project's dependencies
are present even though git does not track them.
"""

TREE_TEMPLATE = """\
# The repository

Every file tracked by git, with its line count in parentheses, so you do not
have to go looking -- and can tell a 600-line file from a 60-line one before
you spend your context on it:

```
{tree}
```
"""

REPAIR_TASK = """\
**This iteration's job is repair, not the plan.** The files listed under "Broken
files" above do not parse, and until they do, the plan is not what matters: an
edit to a file that is already broken tends to break it further, and nothing
that reads it can be trusted. Fix those, leave the plan step for next time, and
say in your handoff what you repaired."""

PLAN_TASK = """\
Do the one step the plan names, and nothing else."""

DEFECTS_TEMPLATE = """\
# Broken files

The harness checked the files this run has changed and found problems that are
almost certainly damage from an edit rather than decisions you made -- a file
that no longer parses, a conflict marker, or a block of lines pasted twice.

```
{problems}
```

These are this iteration's work. They are cheap to repair now and they compound:
later edits to a file that no longer parses tend to make it worse, and nothing
downstream of a broken file can be trusted.
"""

GATE_TEMPLATE = """\
# Commit gate

`{command}` runs after every iteration. Last run: {result}
{output}
"""

FIRST_HANDOFF = """\
This is the first iteration. Nothing has been done yet.

The file list above is your orientation -- you do not need to survey the
codebase before starting. Pick the smallest real change that moves the objective
forward, read only the files that change requires, and make it.
"""


def build(
    *,
    objective: str,
    number: int,
    max_iterations: int,
    branch: str,
    base: str,
    log: str,
    diff: str,
    handoff: str,
    handoff_path: str,
    tree: str = "",
    plan: str = "",
    plan_path: str = "",
    plan_progress: tuple[int, int] = (0, 0),
    linked: list[str] | None = None,
    interpreter: str = "",
    defects: list[str] | None = None,
    gate_command: str = "",
    gate_result: str = "",
    gate_output: str = "",
) -> str:
    tree_section = TREE_TEMPLATE.format(tree=tree.strip()) + "\n" if tree.strip() else ""
    environment_section = _environment(linked or [], interpreter)
    plan_section = _plan(plan, plan_path, plan_progress)
    defects_section = (
        DEFECTS_TEMPLATE.format(problems="\n".join(defects[:15])) + "\n" if defects else ""
    )
    # A broken file outranks the plan: repair is the whole task this iteration.
    task_line = REPAIR_TASK if defects else PLAN_TASK

    gate_section = ""
    if gate_command:
        output = ""
        if gate_result.startswith("fail") and gate_output:
            output = "\n```\n" + gate_output.strip()[-1500:] + "\n```\n"
        gate_section = GATE_TEMPLATE.format(
            command=gate_command,
            result=gate_result or "not yet run",
            output=output,
        ) + "\n"

    return TEMPLATE.format(
        objective=objective.strip(),
        number=number,
        max_iterations=max_iterations,
        branch=branch,
        base=base[:12],
        log=_block(log, "(no commits yet)"),
        diff=_block(diff, "(no changes yet)"),
        tree_section=tree_section,
        environment_section=environment_section,
        plan_section=plan_section,
        defects_section=defects_section,
        task_line=task_line,
        gate_section=gate_section,
        handoff=handoff.strip() or FIRST_HANDOFF,
        handoff_path=handoff_path,
    )


def _plan(plan: str, path: str, progress: tuple[int, int]) -> str:
    """The objective, decomposed, carried between iterations as a file.

    This is the answer to the thing that actually broke every run: a big
    objective and a small window.  Without it every iteration re-derived "what
    should I do now" from the whole objective, which meant surveying the whole
    codebase, which meant overflowing before writing anything -- the same
    failure on two different models at two different context sizes.

    A plan the agent writes and maintains turns "add a test suite covering the
    main /api routes" into "- [ ] tests for GET /api/players", which fits.  It
    is a file for the same reason the handoff is a file: nothing here parses a
    model's prose, and a file survives a context overflow that a chain of
    reasoning does not.
    """
    if not plan.strip():
        return PLAN_TEMPLATE.format(body=NO_PLAN.format(path=path).strip()) + "\n"

    done, total = progress
    if total:
        remaining = total - done
        summary = (
            f"{done} of {total} steps are done, {remaining} to go."
            if remaining
            else f"All {total} steps are checked off -- verify that is really true, "
            "and if the objective is met say so in your handoff."
        )
    else:
        summary = "It has no checkboxes yet; add them as you go."
    body = HAVE_PLAN.format(plan=plan.strip(), path=path, progress=summary).strip()
    return PLAN_TEMPLATE.format(body=body) + "\n"


def _environment(linked: list[str], interpreter: str) -> str:
    """Name the environment the worktree was given.

    A worktree starts with tracked files only, so the loop links the project's
    untracked dependencies in.  Saying so is the other half of the fix: an agent
    that does not know an interpreter exists goes looking for one, and on
    one-project that cost an entire iteration -- 24 tool calls enumerating every
    python3 on the box, none of which had Flask, while the project's own
    virtualenv sat one symlink away.
    """
    parts = []
    if interpreter:
        parts.append(INTERPRETER_LINE.format(interpreter=interpreter))
    others = [name for name in linked if not interpreter.startswith(name + "/")]
    if others:
        parts.append(LINKED_LINE.format(
            names="`, `".join(others),
            verb="is" if len(others) == 1 else "are",
        ))
    if not parts:
        return ""
    return ENVIRONMENT_TEMPLATE.format(body="\n".join(parts).strip()) + "\n"


def _block(text: str, empty: str) -> str:
    body = text.strip() or empty
    return "```\n" + body + "\n```"
