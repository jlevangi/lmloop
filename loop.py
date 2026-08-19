"""The run lifecycle: create a worktree, iterate, commit, stop.

The whole design fits in one sentence: **the loop never discards work.**  the predecessor
calls ``git reset --hard HEAD && git clean -fd`` on any failed iteration, which
is a sound trade when an iteration costs ninety seconds and an unreliable one is
cheaper to redo than to reason about.  At ninety minutes it is not: one run
produced 87 minutes of correct, coherent edits and lost every line because the
agent's final message was not parseable JSON.  The code was fine.  The envelope
was not.

So every iteration that leaves a diff produces a commit, labelled with what
actually happened, and the next iteration continues from it.  `git log` becomes
a truthful record of the run, and anything unwanted is one `git revert` away at
a time of the operator's choosing rather than the loop's.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import checks
import display
import gitops
import models
import prompts
import pi_runner
from rundir import RunDir, make_run_id


class Run:
    def __init__(
        self,
        repo: Path,
        config: dict,
        objective: str,
        max_iterations: int | None = None,
        run_id: str | None = None,
    ):
        self.repo = repo
        self.config = config
        self.objective = objective
        self.run_id = run_id or make_run_id(objective)
        self.max_iterations = max_iterations or config["stop"]["max_iterations"]
        self.model = config["agent"]["model"]
        self.interrupted = False

        self.branch = config["worktree"]["branch"].format(repo=repo.name, run_id=self.run_id)
        self.worktree = Path(
            config["worktree"]["root"].format(repo=str(repo), run_id=self.run_id)
        )
        self.rundir = RunDir(self.worktree, self.run_id)

        self.gate_result = ""
        self.gate_output = ""
        self.no_diff_streak = 0
        self.linked: list[str] = []
        self.defects: list[str] = []
        self.screen = display.Screen()

    # -- setup ------------------------------------------------------------

    def _exclusions(self) -> list[str]:
        """What git must never see, whichever worktree it is standing in.

        `.lmloop.toml` is the operator's config at the repo root; the rest are
        run artifacts and the linked environment.  A symlinked `.venv` is still
        a file as far as `git add -A` is concerned, and committing it would put
        an absolute path to somebody's home directory in the history.
        """
        linkable = list(self.config["worktree"].get("link") or [])
        return [".worktrees/", ".lmloop/", ".lmloop.toml", ".pi/"] + linkable

    def prepare(self) -> None:
        if self.worktree.exists():
            raise SystemExit(f"lmloop: {self.worktree} already exists")

        # `.lmloop.toml` is excluded too: it is the operator's config, it lives
        # at the repo root, and `git add -A` inside a worktree would otherwise
        # sweep it into the run's first commit.
        gitops.exclude(self.repo, self._exclusions())
        gitops.add_worktree(self.repo, self.worktree, self.branch)
        base = gitops.head_commit(self.worktree)
        self.rundir.create(self.objective, base)
        self.publish_sessions()
        self.linked = self.link_environment()
        self.rundir.event(
            "run:start",
            runId=self.run_id,
            runDir=str(self.rundir.path),
            agent="pi",
            model=self.model,
            worktree=True,
            worktreePath=str(self.worktree),
            branch=self.branch,
            baseCommit=base,
            maxIterations=self.max_iterations,
            promptLength=len(self.objective),
        )

    def attach(self, extra_iterations: int) -> int:
        """Re-enter an existing run instead of starting a new one.

        A run that dies -- a reboot, a closed ssh session, an OOM -- leaves its
        commits behind, but starting fresh would build a second worktree and
        abandon the handoff chain that makes the next iteration cheap.  This
        picks the run back up where it stopped: same worktree, same branch, same
        run directory, same handoff.
        """
        if not self.rundir.path.is_dir():
            raise SystemExit(f"lmloop: no run directory at {self.rundir.path}")
        done = max(
            (int(path.stem.split("-")[1]) for path in self.rundir.path.glob("iteration-*-prompt.md")),
            default=0,
        )
        self.objective = (self.rundir.path / "prompt.md").read_text().strip()
        self.max_iterations = done + extra_iterations
        # Runs that predate this, and runs resumed after the exclude list grew.
        gitops.exclude(self.repo, self._exclusions())
        self.publish_sessions()
        self.linked = self.link_environment()
        self.rundir.event(
            "run:start",
            runId=self.run_id,
            runDir=str(self.rundir.path),
            agent="pi",
            model=self.model,
            resumed=True,
            completedIterations=done,
            worktreePath=str(self.worktree),
            branch=self.branch,
            maxIterations=self.max_iterations,
        )
        return done

    # -- stop conditions --------------------------------------------------

    def _abort_reason(self, iteration: int, started: float) -> str | None:
        if self.interrupted:
            return "interrupted"
        if self.rundir.stop_requested():
            return "STOP sentinel present"
        if iteration > self.max_iterations:
            return f"max iterations reached ({self.max_iterations})"
        hours = (time.monotonic() - started) / 3600
        if hours >= self.config["stop"]["max_wall_hours"]:
            return f"max wall clock reached ({hours:.1f}h)"
        limit = self.config["stop"]["no_diff_iterations"]
        if self.no_diff_streak >= limit:
            # Iteration counts and self-reported summaries are not evidence of
            # work.  Git is the only honest witness, so this is the one guard
            # that cannot be talked out of stopping.
            return f"no git-visible change in {limit} consecutive iterations"
        return None

    # -- the environment the worktree does not inherit ---------------------

    def link_environment(self) -> list[str]:
        """Symlink the repo's untracked environment into the worktree.

        See `config.DEFAULTS["worktree"]["link"]` for why this exists and why it
        links rather than copies.  Returns the names actually linked, which the
        prompt then names for the agent -- knowing the interpreter is there is
        worth as much as the interpreter being there, since an agent that cannot
        find one goes looking instead of working.
        """
        linked: list[str] = []
        for name in self.config["worktree"].get("link") or []:
            source = self.repo / name
            target = self.worktree / name
            if not source.exists():
                continue
            if target.is_symlink() or target.exists():
                # Already there, usually from an earlier iteration of this run.
                # Still report it: what the prompt and the header describe is
                # what the worktree HAS, not what this call happened to create.
                linked.append(name)
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(source, target_is_directory=source.is_dir())
            except OSError as error:
                # Never fail a run over this: the agent may not need it, and a
                # missing link is visible in the log either way.
                self.rundir.event("worktree:link:failed", name=name, detail=str(error))
                continue
            linked.append(name)
        if linked:
            self.rundir.event("worktree:link", names=linked)
        return linked

    def interpreter(self) -> str:
        """The project's own Python, if one was linked in.  Repo-relative."""
        for name in self.config["worktree"].get("link") or []:
            candidate = self.worktree / name / "bin" / "python"
            if candidate.exists():
                return f"{name}/bin/python"
        return ""

    # -- discoverability --------------------------------------------------

    def publish_sessions(self) -> None:
        """Point pi-aware tools at this run's transcripts.

        lmloop keeps pi's session files inside the run directory so a run is one
        self-contained artifact.  The cost is that anything looking for pi
        sessions in the usual place finds nothing, and an lmloop run was
        therefore invisible in paseo's import picker -- not because of how the
        run was launched, but because paseo resolves one session directory per
        cwd and walks only that.  Symlinking into the default location does not
        help: its walker tests `isFile()`, which a symlink fails.

        `<cwd>/.pi/settings.json` is the hook that ecosystem already reads for
        exactly this.  One small file, transcripts stay where they are.

        The matching is on the *worktree* path, since that is the cwd pi records
        -- so the worktree is what has to be registered as a paseo workspace,
        not the repo above it.
        """
        settings = self.worktree / ".pi" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(
            json.dumps({"sessionDir": str(self.rundir.sessions)}, indent=2) + "\n"
        )

    # -- environment ------------------------------------------------------

    def env(self) -> dict:
        """The environment for anything that runs inside the worktree.

        Nothing that merely *ran* during an iteration should end up in the
        commit.  Python writes bytecode next to the source by default, so both
        the gate and any `python` the agent invokes through its bash tool leave
        `__pycache__` behind, and `git add -A` sweeps it up.  Redirecting the
        cache into the run directory -- already excluded from git -- keeps the
        commit to what the agent actually wrote.
        """
        return dict(os.environ, PYTHONPYCACHEPREFIX=str(self.rundir.path / "pycache"))

    # -- the gate ---------------------------------------------------------

    def run_gate(self, number: int) -> None:
        command = self.config["gate"]["command"]
        if not command:
            self.gate_result, self.gate_output = "", ""
            return
        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=str(self.worktree),
                capture_output=True,
                text=True,
                timeout=600,
                env=self.env(),
            )
            output = (completed.stdout + completed.stderr).strip()
            self.gate_result = "pass" if completed.returncode == 0 else f"fail (rc={completed.returncode})"
        except subprocess.TimeoutExpired:
            output, self.gate_result = "gate timed out after 600s", "fail (timeout)"
        except OSError as error:
            output, self.gate_result = str(error), "fail (could not run)"
        self.gate_output = output
        self.rundir.gate_log(number).write_text(f"$ {command}\n\n{output}\n")

    # -- one iteration ----------------------------------------------------

    def roles(self) -> tuple[str, str, str]:
        """Which model runs this iteration, and why.

        The first iteration of a run has no plan, so its job is to read the
        repository and decide the steps -- a whole-repository question that
        happens once and wants the widest window available.  Every iteration
        after it carries out one step, which is a two-file question that happens
        constantly and wants throughput.  Those are different models on local
        hardware, and `planner_model` is how a project says so.
        """
        agent = self.config["agent"]
        planning = not self.rundir.read_plan().strip()
        if planning and agent.get("planner_model"):
            return (
                agent["planner_model"],
                agent.get("planner_thinking") or agent.get("thinking", ""),
                "planning",
            )
        return agent["model"], agent.get("thinking", ""), "editing"

    def iterate(self, number: int) -> None:
        base = self.rundir.base_commit
        self.model, thinking, role = self.roles()
        ok, detail = models.preflight(self.model, self.config["models"]["llama_swap_url"])
        self.rundir.event("preflight", iteration=number, ok=ok, detail=detail)
        if not ok:
            raise PreflightError(detail)

        prompt = prompts.build(
            objective=self.objective,
            number=number,
            max_iterations=self.max_iterations,
            branch=self.branch,
            base=base,
            log=gitops.log_oneline(self.worktree, base),
            diff=gitops.diff_stat(self.worktree, base),
            handoff=self.rundir.read_handoff(),
            handoff_path=str(self.rundir.handoff_path),
            tree=gitops.tracked_files(self.worktree),
            plan=self.rundir.read_plan(),
            plan_path=str(self.rundir.plan_path),
            plan_progress=self.rundir.plan_progress(),
            linked=getattr(self, "linked", []),
            interpreter=self.interpreter(),
            gate_command=self.config["gate"]["command"],
            gate_result=self.gate_result,
            gate_output=self.gate_output,
            defects=self.defects,
        )
        self.rundir.iteration_prompt(number).write_text(prompt)
        self.rundir.event(
            "iteration:start",
            iteration=number,
            promptLength=len(prompt),
            preflight=detail,
            model=self.model,
            role=role,
            git={"head": gitops.head_commit(self.worktree), "commitCount": gitops.commit_count(self.worktree, base)},
        )
        self.screen.log(f"  iteration {number}: {detail}")
        if role == "planning":
            self.screen.log(f"    planning with {self.model}")

        self._loading = True
        handoff_before = self.rundir.handoff_mtime()
        self._plan_before = self.rundir.plan_progress()
        self._plan_steps_before = self._plan_steps()
        result = pi_runner.run(
            model=self.model,
            tools=self.config["agent"]["tools"],
            thinking=thinking,
            prompt=prompt,
            cwd=self.worktree,
            session_dir=self.rundir.sessions,
            session_id=f"iter-{number}",
            raw_path=self.rundir.iteration_jsonl(number),
            timeout_seconds=self.config["iteration"]["timeout_seconds"],
            stall_seconds=self.config["iteration"]["stall_seconds"],
            max_compactions=self.config["iteration"]["max_compactions"],
            env=self.env(),
            should_stop=lambda: self.interrupted or self.rundir.stop_requested(),
            on_progress=lambda snap: self._show(number, snap),
        )

        self.run_gate(number)
        # Structural checks run whatever the project configured, because the
        # damage an edit does is not project-specific -- see checks.py.
        self.defects = checks.run(self.worktree, base)
        if self.defects:
            self.rundir.event("checks:failed", iteration=number, problems=self.defects[:20])

        handoff_written = self.rundir.handoff_mtime() > handoff_before
        if not handoff_written:
            # An iteration that overflowed its context did write a handoff -- into
            # pi's event stream rather than to disk.  Prefer it over a git diff
            # that, for the iterations this happens to, is empty.
            self.rundir.write_synthetic_handoff(
                number,
                gitops.diff_shortstat(self.worktree, base),
                carried=self.rundir.last_compaction_summary(number),
            )
        summary = self._subject(number, result, handoff_written)

        commit = self.commit(number, summary, result, handoff_written, base)
        self.record(number, summary, result, handoff_written, commit, base)

    def _subject(self, number: int, result, handoff_written: bool) -> str:
        """One line describing what this iteration did, for the commit subject.

        Line 1 of the handoff is the right answer when the agent wrote one.  It
        is the wrong answer when it did not: the loop then synthesises a handoff
        whose first line says "iteration N ended without writing a handoff", and
        that became the subject of four of nine commits on one-project --
        iterations that wrote real, working test files and described none of it
        in `git log`.  Overflowing the context is exactly when the agent fails to
        write a handoff, so the commits most in need of a subject were the ones
        guaranteed not to get one.

        So a synthesised handoff is not trusted for the subject.  Fall back, in
        order, to the plan step that got checked off this iteration, then to the
        files that changed, then to the outcome.  All three are observations, not
        self-reports.
        """
        if handoff_written:
            first = self.rundir.read_handoff().splitlines()
            if first and first[0].strip():
                return first[0].strip()

        step = self._completed_step()
        if step:
            return step

        changed = self._relative_files(result.files_touched)
        if changed:
            listed = ", ".join(changed[:3])
            more = f" (+{len(changed) - 3} more)" if len(changed) > 3 else ""
            return f"{listed}{more}"

        return f"iteration {number} ({result.outcome})"

    def _completed_step(self) -> str:
        """The plan step checked off during this iteration, if exactly one was.

        Ambiguity is not worth guessing at: if the agent checked off two steps,
        neither is "the" subject, and the file list describes the commit better.
        """
        before = getattr(self, "_plan_steps_before", set())
        after = self._plan_steps()
        gained = [text for text in after - before]
        return gained[0][:68] if len(gained) == 1 else ""

    def _plan_steps(self) -> set[str]:
        """The text of every checked-off plan step, right now."""
        done = set()
        for line in self.rundir.read_plan().splitlines():
            stripped = line.strip()
            if stripped.startswith(("- [", "* [")) and stripped[3:4].lower() == "x":
                done.add(stripped[5:].strip().strip("`"))
        return done

    def _show(self, number: int, snap: dict) -> None:
        """One line that keeps moving, so an attached terminal never looks dead."""
        # A model load is a one-off event with a duration, not a state worth
        # repeating every five seconds.  Report it once, when it ends.
        if not snap["loading"] and self._loading:
            self._loading = False
            self.screen.log(f"    model ready in {display.elapsed(snap['elapsed'])}")

        paint = self.screen.paint
        # The spinner advances only while pi is emitting.  A model that has gone
        # quiet freezes it, so "moving" means the agent is alive rather than
        # merely that the loop redrew.
        alive = snap["loading"] or snap["quiet"] <= 30
        spinner = self.screen.spin(advance=alive)

        if snap["loading"]:
            detail = paint.yellow("loading model")
        elif snap["quiet"] > 60:
            detail = paint.yellow(f"quiet {display.elapsed(snap['quiet'])}")
        elif snap["last_tool"]:
            target = snap.get("last_target", "")
            detail = paint.cyan(snap["last_tool"] + (f" {target}" if target else ""))
        else:
            detail = paint.dim("thinking")

        plan_done, plan_total = self.rundir.plan_progress()
        flags = "".join(
            flag
            for flag, on in (("PAUSE", self.rundir.paused()), ("STOP", self.rundir.stop_requested()))
            if on
        )
        # List order is layout; the number is what survives a narrow terminal.
        # On a phone the useful answer is "which iteration, doing what, and is it
        # stopping" -- counters and even elapsed time go before those do.
        self.screen.status([
            # The spinner rides with the iteration counter rather than as its own
            # segment: it must never be the thing `compose` drops, because on the
            # narrowest terminal it is the only proof the run is alive.
            (6, f"{paint.cyan(spinner)} {paint.bold(f'{number}/{self.max_iterations}')}"),
            (4, display.elapsed(snap["elapsed"])),
            (5, detail),
            # Only shown once it has happened, and then it outranks the counters:
            # a climbing overflow count against zero writes is the difference
            # between an iteration that is working and one that is going in
            # circles, and it is not visible anywhere else while the run is live.
            (3, paint.red(f"{snap['compactions']} overflow") if snap["compactions"] else ""),
            # What a long run actually looks like from outside: not tool calls,
            # but how much of the plan is behind it.
            (4, paint.green(f"{plan_done}/{plan_total} steps") if plan_total else ""),
            (2, paint.dim(f"{snap['tool_calls']} tools")),
            (1, paint.dim(f"{snap['output_tokens']} out")),
            (7, paint.red(f"[{flags}]") if flags else ""),
        ])
        self.rundir.write_status({
            "run_id": self.run_id,
            "iteration": number,
            "max_iterations": self.max_iterations,
            "model": self.model,
            "phase": "loading" if snap["loading"] else "working",
            "elapsed_seconds": round(snap["elapsed"]),
            "last_tool": snap["last_tool"],
            "last_target": snap.get("last_target", ""),
            "tool_calls": snap["tool_calls"],
            "writes": snap["writes"],
            "compactions": snap["compactions"],
            "plan_done": plan_done,
            "plan_total": plan_total,
            "output_tokens": snap["output_tokens"],
            "quiet_seconds": round(snap["quiet"]),
            "paused": self.rundir.paused(),
            "stopping": self.rundir.stop_requested(),
        })

    # -- commit -----------------------------------------------------------

    def commit(self, number: int, summary: str, result, handoff_written: bool, base: str) -> str | None:
        # Line 1 of the handoff is written by a model and is not always one
        # line's worth of text, so the subject is trimmed to something `git log
        # --oneline` can show.  The full text survives in notes.md.
        subject = " ".join(summary.split())
        if len(subject) > 68:
            subject = subject[:65].rstrip(" ,.;:") + "..."

        lines = [
            f"lmloop {self.run_id} iter {number}: {subject}",
            "",
            f"outcome: {result.outcome}" + (f" ({result.detail})" if result.detail else ""),
            f"model:   {self.model}",
        ]
        if self.config["gate"]["command"]:
            lines.append(f"gate:    {self.config['gate']['command']} -> {self.gate_result}")
        if not handoff_written:
            lines.append("handoff: missing (synthesised from git)")
        shortstat = gitops.diff_shortstat(self.worktree, base).strip()
        if shortstat:
            lines.append(f"files:  {shortstat}")

        blocked = self.config["gate"]["blocks_commit"] and self.gate_result.startswith("fail")
        if blocked:
            self.rundir.event("git:commit:blocked", iteration=number, gate=self.gate_result)
            self.screen.log(f"    gate failed and blocks_commit is set; leaving {number} uncommitted")
            return None

        sha = gitops.commit_all(self.worktree, "\n".join(lines) + "\n")
        if sha:
            self.rundir.event("git:commit", iteration=number, sha=sha, outcome=result.outcome)
        return sha

    # -- bookkeeping ------------------------------------------------------

    def _relative_files(self, paths: list[str]) -> list[str]:
        """Repo-relative source paths only.

        The agent's write to handoff.md is a tool call like any other, so it
        turns up in the event stream; it is bookkeeping, not a code change, and
        listing it under "Changes" makes every iteration look like it touched an
        extra file.
        """
        relative = []
        for path in paths:
            candidate = Path(path)
            try:
                candidate = candidate.relative_to(self.worktree)
            except ValueError:
                pass
            if str(candidate).startswith(".lmloop/"):
                continue
            if str(candidate) not in relative:
                relative.append(str(candidate))
        return relative[:20]

    def record(self, number: int, summary: str, result, handoff_written: bool, commit: str | None, base: str) -> None:
        prefix = "" if result.outcome == "ok" else f"[{result.outcome.upper()}] "
        changes = self._relative_files(result.files_touched)
        learnings = []
        if self.gate_result:
            learnings.append(f"gate `{self.config['gate']['command']}` -> {self.gate_result}")
        done, total = self.rundir.plan_progress()
        if total:
            gained = done - getattr(self, '_plan_before', (0, 0))[0]
            learnings.append(
                f"plan: {done}/{total} steps done"
                + (f" (+{gained} this iteration)" if gained > 0 else " (no step completed)")
            )
        elif self.rundir.read_plan():
            learnings.append("plan exists but has no checkboxes")
        else:
            learnings.append("no plan written; the objective was never broken down")
        for defect in self.defects[:6]:
            learnings.append(f"structural check: {defect}")
        if result.compactions:
            learnings.append(
                f"context overflowed {result.compactions}x; every overflow costs the"
                " agent everything it had read"
            )
        if not handoff_written and result.compactions:
            learnings.append("agent wrote no handoff; the loop carried its last context summary forward")
        elif not handoff_written:
            learnings.append("agent wrote no handoff; the loop synthesised one from git")
        if result.detail and result.outcome != "ok":
            learnings.append(result.detail)
        self.rundir.append_notes(number, prefix + summary, changes, learnings)

        if commit is None and not gitops.has_uncommitted(self.worktree):
            self.no_diff_streak += 1
        else:
            self.no_diff_streak = 0

        self.rundir.event(
            "iteration:end",
            iteration=number,
            elapsedMs=int(result.elapsed_seconds * 1000),
            success=result.outcome == "ok",
            outcome=result.outcome,
            summary=summary,
            toolCalls=result.tool_calls,
            writes=result.writes,
            compactions=result.compactions,
            handoffWritten=handoff_written,
            planDone=done,
            planTotal=total,
            commit=commit,
            gate=self.gate_result,
            totalInputTokens=result.input_tokens,
            totalOutputTokens=result.output_tokens,
            commitCount=gitops.commit_count(self.worktree, base),
        )
        # The tool-call count is context, not evidence.  An agent can edit a
        # file perfectly well through its bash tool -- one interrupted run here
        # showed "0 writes" against 8 committed insertions -- so what gets
        # reported is what git saw, not what the event stream counted.
        if commit:
            changed = gitops.commit_shortstat(self.worktree, commit) or "no file changes"
            verdict = f"committed {commit[:8]}: {changed}"
        else:
            verdict = "nothing to commit"
        # The overflow count earns its place on this line whenever it is not
        # zero: it is the difference between an iteration that ran out of time
        # and one that ran out of room, and those want opposite responses.
        paint = self.screen.paint
        overflows = paint.red(f" | {result.compactions} overflows") if result.compactions else ""
        if self.defects:
            self.screen.log(paint.red(f"    {len(self.defects)} structural problem(s) in changed files:"))
            for defect in self.defects[:4]:
                self.screen.log(f"      {defect}")
        outcome = (paint.green if result.outcome == "ok" else paint.yellow)(result.outcome)
        self.screen.log(
            f"    {outcome} in {display.elapsed(result.elapsed_seconds)}"
            f" | {result.tool_calls} tool calls{overflows} | {verdict}"
        )

    # -- driver -----------------------------------------------------------

    def start(self, from_iteration: int = 0) -> int:
        started = time.monotonic()
        signal.signal(signal.SIGINT, self._on_interrupt)
        signal.signal(signal.SIGTERM, self._on_interrupt)

        keys = display.Keys(self.rundir, self.screen)
        keys.start()
        if self.screen.tty:
            self.screen.log(f"  {display.Keys.HELP}")

        iteration = from_iteration
        reason = None
        while True:
            iteration += 1
            display.wait_while_paused(self.rundir, self.screen, lambda: self.interrupted)
            reason = self._abort_reason(iteration, started)
            if reason:
                break
            try:
                self.iterate(iteration)
            except PreflightError as error:
                if not self._backoff(iteration, str(error)):
                    reason = f"preflight failed: {error}"
                    break
                iteration -= 1

        self.rundir.event(
            "run:complete",
            status=reason or "complete",
            iterations=iteration - 1,
            commitCount=gitops.commit_count(self.worktree, self.rundir.base_commit),
            worktreePath=str(self.worktree),
        )
        self.screen.close()
        self._summarise(reason, started)
        return 0

    def _on_interrupt(self, *_args) -> None:
        if self.interrupted:
            raise KeyboardInterrupt
        self.interrupted = True
        self.screen.log("  stop requested; finishing the current iteration")

    def _backoff(self, iteration: int, detail: str) -> bool:
        """1m, 2m, 4m, then give up.  Only for a llama-swap that is not there."""
        if not hasattr(self, "_errors"):
            self._errors = 0
        self._errors += 1
        if self._errors > 3:
            return False
        delay = 60 * 2 ** (self._errors - 1)
        self.rundir.event("backoff:start", iteration=iteration, seconds=delay, detail=detail)
        self.screen.log(f"    {detail}; retrying in {delay // 60}m")
        for _ in range(delay):
            if self.interrupted:
                return False
            time.sleep(1)
        return True

    def _summarise(self, reason: str | None, started: float) -> None:
        base = self.rundir.base_commit
        # Paths are shown relative to the repo, and the commands assume you are
        # standing in it.  Absolute paths here ran to 217 characters, which wraps
        # into noise on a phone -- and the worktree is inside the repo anyway.
        def relative(path: Path) -> str:
            try:
                return str(path.relative_to(self.repo))
            except ValueError:
                return str(path)

        self.screen.log()
        self.screen.log(f"  run stopped: {reason or 'complete'}")
        self.screen.log(f"  {gitops.commit_count(self.worktree, base)} commits on {self.branch}"
                        f" in {(time.monotonic() - started) / 3600:.1f}h")
        self.screen.log()
        # The notes path relative to the *worktree*, not the repo: relative to the
        # repo it repeats the run id twice and reaches 130 characters, which is
        # four wrapped lines on a phone for one file name.  The worktree it hangs
        # off is printed directly above it.
        try:
            notes = self.rundir.notes_path.relative_to(self.worktree)
        except ValueError:
            notes = self.rundir.notes_path
        self.screen.log(f"  worktree  {relative(self.worktree)}")
        self.screen.log(f"  notes     {notes}  (inside it)")
        self.screen.log()
        self.screen.log(f"  from {self.repo}:")
        self.screen.log(f"    git log --oneline {base[:8]}..{self.branch}")
        self.screen.log(f"    git merge {self.branch}")


class PreflightError(RuntimeError):
    pass
