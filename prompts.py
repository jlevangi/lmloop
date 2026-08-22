"""The iteration prompt.

A fresh session every iteration bounds context against a hard 65,536-token
window, but it also means the agent wakes up knowing nothing.  One observed
iteration spent 43 file reads just working out where it was -- at ~15
tok/s that is a large fraction of an iteration spent re-deriving facts the loop
already has.  So the loop pays that cost once, in Python, and hands the answer
over.

The first version of this prompt precomputed the git log and the diff, and that
was not enough: **both are empty on iteration 1 of a fresh run**, so the warm
prompt was coldest exactly when the agent was.  Nothing in it described the
shape of the repository, and on one project the agent bought that shape with 18
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

The other job here is the handoff instruction.  The predecessor demands the final
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

{environment_section}{history_section}{tree_section}{gate_section}{defects_section}{thrash_section}{plan_section}# Handoff from the previous iteration

{handoff}

# What to do now

{task_line} You are one iteration in a
long chain: the objective is not yours to finish today, and there is no credit
for getting further than the step. The run is measured in commits, so one small
finished step beats three half-finished ones.

**Read at most {file_limit_words} files before your first write.** This is a hard budget, not
advice. Your context window is smaller than this codebase: if you fill it with
file contents before writing anything, it overflows, you lose everything you
read, and you start over from a summary -- three times in a row on one observed
iteration, forty file reads, not one line written. Surveying everything you
might need is the single most reliable way to finish an iteration with nothing.

So: read only the files the next change actually requires, write the change,
and only then read more. A rough file you improve next iteration is worth more
than a perfect plan you never got to type, because the rough file is committed
and the plan dies with your context. If the objective is bigger than
{file_limit_words} files, split it in the plan and do the first piece.

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
tells you what exists, and that is all planning needs.  Write it to `{path}`,
starting with a heading that names this run in two to five words, then the steps
as markdown checkboxes, most useful first:

    # Dark mode across every screen

    - [ ] one small step
    - [ ] the next one
    - [ ] ...

The heading is the name this run is listed under, so make it the shortest thing
that distinguishes it from the other work on this repository.  Name the work,
not the activity: "Currency and date formatting" tells someone which run this is
and "Improve the code" does not.  Do not write the word "Plan", the date, or the
objective back verbatim -- it is a label, not a summary.

Then do the FIRST step, check it off, and stop.  You are not expected to finish
the objective this iteration, or in the next five.  There will be many more.
"""

HAVE_PLAN = """\
This is the plan for the objective, as you left it.  {progress}

```
{plan}
```

Do up to {steps_per_iteration} unchecked {step_word}, in order. When each is done,
edit `{path}` to check it off. Stop after that limit even if more work is ready.

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

HISTORY_TEMPLATE = """\
# Earlier runs on this repository

Other objectives have been worked on here. You start in a fresh worktree and
would otherwise rediscover this repository from scratch, including the dead
ends, so here is what happened before:

{runs}

Treat it as background, not instruction. Your objective is the one at the top of
this prompt, and anything here that contradicts what you find in the files is
out of date -- the files win.
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

SPLIT_TASK = """\
**This iteration's job is to split one plan step, then do the first piece.**
The step named under "A step that will not fit" above has already defeated the
context window {times}x. Attempting it again as written is attempting the thing
that just failed, for a reason that has not changed."""

THRASH_TEMPLATE = """\
# A step that will not fit

```
{step}
```

{times_line} the iteration read files until the context window overflowed,
compacted to a summary, and overflowed again without writing a line. That is the
window losing to the codebase, not the step being wrong: the work is real, there
is just more of it in view at once than there is room for.

The fix is to make the step smaller **in what it has to read**, which is not the
same as smaller in what it has to do. Restating one goal as three sub-goals
changes nothing, because each of the three still needs the same files open at
once. Splitting the step by *file* does change something.

So, before anything else, edit `{plan_path}`: replace that one step with two or
more that each name the specific file they touch, ordered so the first can be
done knowing only that file. Then do the first of them. Leave the rest checked
off by later iterations.

If the step genuinely cannot be split -- one file is simply too large to read --
then say so in your handoff and change the step to work on a named region of it
instead: a single function, a single selector block, a single component.
"""

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
{inherited}{output}
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
    history: list[dict] | None = None,
    plan: str = "",
    plan_path: str = "",
    plan_progress: tuple[int, int] = (0, 0),
    linked: list[str] | None = None,
    interpreter: str = "",
    defects: list[str] | None = None,
    gate_command: str = "",
    gate_result: str = "",
    gate_output: str = "",
    gate_baseline: str = "",
    thrashed_step: str = "",
    thrashed_times: int = 0,
    planning: dict | None = None,
) -> str:
    tree_section = TREE_TEMPLATE.format(tree=tree.strip()) + "\n" if tree.strip() else ""
    environment_section = _environment(linked or [], interpreter)
    history_section = _history(history or [])
    planning = planning or {}
    file_limit = max(1, int(planning.get("pre_write_file_limit", 3)))
    steps_per_iteration = max(1, int(planning.get("steps_per_iteration", 1)))
    plan_section = _plan(plan, plan_path, plan_progress, steps_per_iteration)
    defects_section = (
        DEFECTS_TEMPLATE.format(problems="\n".join(defects[:15])) + "\n" if defects else ""
    )
    thrash_section = ""
    if thrashed_step and thrashed_times:
        times_line = (
            "Twice now," if thrashed_times == 2
            else "Last iteration," if thrashed_times < 2
            else f"{thrashed_times} times now,"
        )
        thrash_section = THRASH_TEMPLATE.format(
            step=thrashed_step.strip(),
            times_line=times_line,
            plan_path=plan_path or "the plan",
        ) + "\n"

    # A broken file outranks the plan -- repair is the whole task this iteration,
    # because editing around a file that does not parse only makes it worse.
    # A step that will not fit outranks doing that step, for the same shape of
    # reason: attempting it again as written is attempting what just failed.
    task_line = REPAIR_TASK if defects else SPLIT_TASK.format(times=thrashed_times) if thrash_section else PLAN_TASK

    gate_section = ""
    if gate_command:
        output = ""
        # Anything that is not a pass is worth showing, including a gate that
        # could not be run at all -- that output is the only thing that says why.
        if gate_result and not gate_result.startswith("pass") and gate_output:
            output = "\n```\n" + gate_output.strip()[-1500:] + "\n```\n"
        inherited = ""
        if gate_result and gate_result == gate_baseline and not gate_result.startswith("pass"):
            # The gate failed the same way before this run touched anything, so
            # it is the repository's failure to own or ignore -- not evidence
            # that this iteration broke something, and not a reason to spend the
            # hour chasing it unless the objective says to.
            inherited = (
                "\nIt failed identically on the base commit, before this run"
                " changed anything.\n"
            )
        gate_section = GATE_TEMPLATE.format(
            command=gate_command,
            result=gate_result or "not yet run",
            inherited=inherited,
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
        history_section=history_section,
        plan_section=plan_section,
        defects_section=defects_section,
        thrash_section=thrash_section,
        task_line=task_line,
        gate_section=gate_section,
        handoff=handoff.strip() or FIRST_HANDOFF,
        handoff_path=handoff_path,
        file_limit_words=_number_word(file_limit),
    )


def _plan(plan: str, path: str, progress: tuple[int, int], steps_per_iteration: int = 1) -> str:
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
    body = HAVE_PLAN.format(
        plan=plan.strip(), path=path, progress=summary,
        steps_per_iteration=steps_per_iteration,
        step_word="step" if steps_per_iteration == 1 else "steps",
    ).strip()
    return PLAN_TEMPLATE.format(body=body) + "\n"


def _number_word(value: int) -> str:
    return {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
            7: "seven", 8: "eight", 9: "nine", 10: "ten"}.get(value, str(value))


def _history(runs: list[dict]) -> str:
    """A few lines per earlier run: what it tried, whether it landed.

    Outcomes are listed rather than summarised because they mean different
    things -- a run of `thrashing` says the objective was too big for the window
    here, which is worth knowing before writing a plan against the same
    codebase.
    """
    if not runs:
        return ""
    lines = []
    for run in runs:
        verdict = f"{run['iterations']} iterations, {run['commits']} commits"
        failures = [o for o in run.get("outcomes", []) if o not in ("ok", "")]
        if failures:
            counts: dict[str, int] = {}
            for outcome in failures:
                counts[outcome] = counts.get(outcome, 0) + 1
            verdict += " (" + ", ".join(f"{n}x {o}" for o, n in counts.items()) + ")"
        lines.append(f"- **{run['objective']}**\n  {verdict}")
        if run.get("handoff") and "without writing a handoff" not in run["handoff"]:
            lines.append(f"  ended: {run['handoff']}")
    return HISTORY_TEMPLATE.format(runs="\n".join(lines)) + "\n"


def _environment(linked: list[str], interpreter: str) -> str:
    """Name the environment the worktree was given.

    A worktree starts with tracked files only, so the loop links the project's
    untracked dependencies in.  Saying so is the other half of the fix: an agent
    that does not know an interpreter exists goes looking for one, and on one
    Flask project that cost an entire iteration -- 24 tool calls enumerating every
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
