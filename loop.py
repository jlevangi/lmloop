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

import os
import signal
import subprocess
import time
from pathlib import Path

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

    # -- setup ------------------------------------------------------------

    def prepare(self) -> None:
        if self.worktree.exists():
            raise SystemExit(f"lmloop: {self.worktree} already exists")

        gitops.exclude(self.repo, [".worktrees/", ".lmloop/"])
        gitops.add_worktree(self.repo, self.worktree, self.branch)
        base = gitops.head_commit(self.worktree)
        self.rundir.create(self.objective, base)
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

    def iterate(self, number: int) -> None:
        base = self.rundir.base_commit
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
            gate_command=self.config["gate"]["command"],
            gate_result=self.gate_result,
            gate_output=self.gate_output,
        )
        self.rundir.iteration_prompt(number).write_text(prompt)
        self.rundir.event(
            "iteration:start",
            iteration=number,
            promptLength=len(prompt),
            preflight=detail,
            git={"head": gitops.head_commit(self.worktree), "commitCount": gitops.commit_count(self.worktree, base)},
        )
        print(f"  iteration {number}: {detail}", flush=True)

        handoff_before = self.rundir.handoff_mtime()
        result = pi_runner.run(
            model=self.model,
            tools=self.config["agent"]["tools"],
            prompt=prompt,
            cwd=self.worktree,
            session_dir=self.rundir.sessions,
            session_id=f"iter-{number}",
            raw_path=self.rundir.iteration_jsonl(number),
            timeout_seconds=self.config["iteration"]["timeout_seconds"],
            stall_seconds=self.config["iteration"]["stall_seconds"],
            env=self.env(),
            should_stop=lambda: self.interrupted or self.rundir.stop_requested(),
        )

        self.run_gate(number)

        handoff_written = self.rundir.handoff_mtime() > handoff_before
        if not handoff_written:
            self.rundir.write_synthetic_handoff(number, gitops.diff_shortstat(self.worktree, base))
        summary = self.rundir.read_handoff().splitlines()[0].strip() if self.rundir.read_handoff() else ""
        summary = summary or f"iteration {number} ({result.outcome})"

        commit = self.commit(number, summary, result, handoff_written, base)
        self.record(number, summary, result, handoff_written, commit, base)

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
            print(f"    gate failed and blocks_commit is set; leaving {number} uncommitted", flush=True)
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
        if not handoff_written:
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
            handoffWritten=handoff_written,
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
        print(
            f"    {result.outcome} in {result.elapsed_seconds / 60:.0f}m"
            f" | {result.tool_calls} tool calls | {verdict}",
            flush=True,
        )

    # -- driver -----------------------------------------------------------

    def start(self, from_iteration: int = 0) -> int:
        started = time.monotonic()
        signal.signal(signal.SIGINT, self._on_interrupt)
        signal.signal(signal.SIGTERM, self._on_interrupt)

        iteration = from_iteration
        reason = None
        while True:
            iteration += 1
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
        self._summarise(reason, started)
        return 0

    def _on_interrupt(self, *_args) -> None:
        if self.interrupted:
            raise KeyboardInterrupt
        self.interrupted = True
        print("\n  stop requested; finishing the current iteration", flush=True)

    def _backoff(self, iteration: int, detail: str) -> bool:
        """1m, 2m, 4m, then give up.  Only for a llama-swap that is not there."""
        if not hasattr(self, "_errors"):
            self._errors = 0
        self._errors += 1
        if self._errors > 3:
            return False
        delay = 60 * 2 ** (self._errors - 1)
        self.rundir.event("backoff:start", iteration=iteration, seconds=delay, detail=detail)
        print(f"    {detail}; retrying in {delay // 60}m", flush=True)
        for _ in range(delay):
            if self.interrupted:
                return False
            time.sleep(1)
        return True

    def _summarise(self, reason: str | None, started: float) -> None:
        base = self.rundir.base_commit
        print()
        print(f"  run {self.run_id} stopped: {reason or 'complete'}")
        print(f"  {gitops.commit_count(self.worktree, base)} commits on {self.branch}"
              f" in {(time.monotonic() - started) / 3600:.1f}h")
        print(f"  worktree: {self.worktree}")
        print(f"  notes:    {self.rundir.notes_path}")
        print()
        print(f"  review:   git -C {self.repo} log --oneline {base[:8]}..{self.branch}")
        print(f"  merge:    git -C {self.repo} merge {self.branch}")


class PreflightError(RuntimeError):
    pass
