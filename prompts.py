"""The iteration prompt.

A fresh session every iteration bounds context against a hard 65,536-token
window, but it also means the agent wakes up knowing nothing.  One observed
the predecessor iteration spent 43 file reads just working out where it was -- at ~15
tok/s that is a large fraction of an iteration spent re-deriving facts the loop
already has.  So the loop pays that cost once, in Python, and hands the answer
over.

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

{gate_section}
# Handoff from the previous iteration

{handoff}

# What to do now

Do ONE coherent unit of work toward the objective. Prefer finishing something
small over starting something large: the run is measured in commits, and an
iteration that half-finishes three things is worth less than one that finishes
one.

Rules:

- Do NOT run `git commit`, `git reset`, or `git checkout`. The harness commits
  everything you leave behind, whether or not you finish cleanly.
- Do NOT edit anything under `.lmloop/` except `handoff.md`.
- Before you finish, write `{handoff_path}` using your write tool. Line 1 must
  be a single-line summary of what you did this iteration -- it becomes the
  commit subject. After that, in whatever form is useful: what you changed,
  what you learned about this codebase, and what the next iteration should do
  first. Be specific about file paths; the next iteration starts with no memory
  of this one and only this file to go on.
"""

GATE_TEMPLATE = """\
# Commit gate

`{command}` runs after every iteration. Last run: {result}
{output}
"""

FIRST_HANDOFF = """\
This is the first iteration. Nothing has been done yet.

Start by orienting yourself in the codebase, then make the smallest real change
that moves the objective forward.
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
    gate_command: str = "",
    gate_result: str = "",
    gate_output: str = "",
) -> str:
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
        gate_section=gate_section,
        handoff=handoff.strip() or FIRST_HANDOFF,
        handoff_path=handoff_path,
    )


def _block(text: str, empty: str) -> str:
    body = text.strip() or empty
    return "```\n" + body + "\n```"
