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
from rundir import RunDir, make_run_id, previous_runs


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
        self.max_iterations = max_iterations or config["stop"]["max_iterations"]
        self.model = config["agent"]["model"]
        self.thinking = config["agent"].get("thinking", "")
        self.role = "editing"
        self.window, self.max_output = models.declared_window(self.model) or (0, 0)
        self.interrupted = False

        # An explicit run id is one somebody else already resolved -- `lmloop
        # resume` naming the run it means, or `--detach`'s parent naming the lane
        # it picked -- so it is taken literally.  A derived one is a new attempt,
        # and gets a free lane if today already used that name.
        derived = make_run_id(objective) if objective.strip() else ""
        self.run_id = run_id if run_id else self._free_run_id(derived)
        # Computed by comparison rather than remembered, so it is true however
        # the id arrived: a detached run is told its id by its parent, and would
        # otherwise never mention the run it stepped around.
        self.collided_with = derived if derived and self.run_id != derived else ""

        self.branch = self._branch_for(self.run_id)
        self.worktree = self._worktree_for(self.run_id)
        self.rundir = RunDir(self.worktree, self.run_id)

        self.gate_result = ""
        self.gate_output = ""
        self.gate_baseline = ""
        self.last_outcome = ""
        self.last_detail = ""
        self.last_commit: str | None = None
        # How many iterations each plan step has thrashed on, keyed by the step
        # text.  A step that has defeated the window twice is a step to split,
        # and the agent is the one who can split it.
        self.thrashed_steps: dict[str, int] = {}
        self.no_diff_streak = 0
        self.linked: list[str] = []
        self.defects: list[str] = []
        self.screen = display.Screen()

    def _branch_for(self, run_id: str) -> str:
        return self.config["worktree"]["branch"].format(repo=self.repo.name, run_id=run_id)

    def _worktree_for(self, run_id: str) -> Path:
        return Path(
            self.config["worktree"]["root"].format(repo=str(self.repo), run_id=run_id)
        )

    def _free_run_id(self, base: str) -> str:
        """``base``, or ``base-2``, ``base-3`` ... if those names are taken.

        The run id is date + slug + hash of the prompt, so the same objective on
        the same day derives the same id -- and that is exactly the day it
        happens, because the reason to re-run an objective is that the first
        attempt went nowhere and the prompt or the config has been fixed since.
        Failing there sent the operator to `git worktree remove` to get on with
        the thing they had just decided to do again.

        Nothing is discarded to make room: the earlier run keeps its worktree,
        its branch and its handoff chain, and the new attempt simply takes the
        next name.  The caller is told which run it stepped around, so that an
        operator who meant `lmloop resume` finds out before the second worktree
        is a surprise.
        """
        if not self._taken(base):
            return base
        for suffix in range(2, 100):
            candidate = f"{base}-{suffix}"
            if not self._taken(candidate):
                return candidate
        return base  # 99 attempts today; let prepare() say so plainly.

    def _taken(self, run_id: str) -> bool:
        """A name is taken if either half of it is: worktree or branch.

        Checking only the directory would hand back a name whose branch still
        exists from a worktree someone removed by hand, and `git worktree add`
        would then fail on the branch instead -- the same dead end, one step
        later and with a worse message.
        """
        return (
            self._worktree_for(run_id).exists()
            or gitops.branch_exists(self.repo, self._branch_for(run_id))
        )

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
            # Only reachable when `_free_run_id` ran out of lanes, or when a run
            # id was named explicitly.  Either way the two real options are
            # continuing that run or clearing it out, so say both, with the
            # commands: this used to be a dead end that the operator had to
            # reverse-engineer from a path.
            raise SystemExit(
                f"lmloop: {self.worktree} already exists\n"
                f"  continue it:  lmloop resume {self.run_id}\n"
                f"  or clear it:  git worktree remove {self.worktree}"
                f" && git branch -D {self.branch}"
            )

        # `.lmloop.toml` is excluded too: it is the operator's config, it lives
        # at the repo root, and `git add -A` inside a worktree would otherwise
        # sweep it into the run's first commit.
        gitops.exclude(self.repo, self._exclusions())
        gitops.add_worktree(self.repo, self.worktree, self.branch)
        base = gitops.head_commit(self.worktree)
        self.rundir.create(self.objective, base)
        self.rundir.claim()
        self.publish_sessions()
        self.linked = self.link_environment()
        self.probe_gate(base)
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
        holder = self.rundir.holder()
        if holder:
            # Two loops in one worktree commit over each other and write the
            # same status file.  A paused run still has a loop; resuming beside
            # it is the mistake this catches.
            raise SystemExit(
                f"lmloop: run {self.run_id} already has a loop (pid {holder})\n"
                f"  it may just be paused:  rm {self.rundir.pause_path}\n"
                f"  or stop it first:       touch {self.rundir.stop_path}"
            )
        self.rundir.claim()
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
        if self.rundir.stop_now_requested():
            return "STOP-NOW sentinel present"
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
            # that cannot be talked out of stopping -- plan progress deliberately
            # does NOT reset the streak, because checking a box is a self-report
            # and an agent that could reset this guard by doing so would be able
            # to talk its way past the only check that never lies.
            #
            # But the operator needs to tell a stuck run from a clean stop, and
            # those look identical in the streak alone: a plan can legitimately
            # contain steps that change no files -- "verify the toggle still
            # works" is real work with no diff.  So the plan movement is
            # reported alongside, as context rather than as permission.
            reason = f"no git-visible change in {limit} consecutive iterations"
            done, total = self.rundir.plan_progress()
            start = getattr(self, "_plan_at_start", None)
            if total and start is not None and done > start:
                reason += f" (plan advanced {start}/{total} -> {done}/{total}, so this may be steps that need no code)"
            elif total:
                reason += f" (plan still at {done}/{total})"
            return reason
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

    def probe_gate(self, base: str) -> None:
        """Run the gate once, on the untouched worktree, before any iteration.

        A gate whose command cannot be found fails identically every iteration,
        and reads as a broken project rather than a broken path -- the one-project
        run recorded `fail (rc=127)` twelve times for a `.venv` that was simply
        not in the worktree, and nothing surfaced it but the event log.  One run
        against the base commit separates the two questions for good, and it is
        cheap next to the hour that follows it.

        It also answers a second question worth having: whether the gate was
        already failing before the agent touched anything.  That failure belongs
        to the repository, not to the run, and an iteration that inherits it
        should not read as the iteration that caused it.

        Only an unrunnable gate stops the run.  A gate that runs and fails is a
        fact about the repository, and refusing to start on it would make lmloop
        useless for the case it is most wanted in: a project that is broken.
        """
        command = self.config["gate"]["command"]
        if not command:
            return
        self.run_gate(0)
        self.gate_baseline = self.gate_result
        self.rundir.event(
            "gate:probe", command=command, result=self.gate_result, baseCommit=base
        )
        if self.gate_result.startswith("misconfigured") or self.gate_result == "fail (could not run)":
            raise SystemExit(
                f"lmloop: the gate cannot be run, so every iteration would record the"
                f" same failure\n"
                f"  gate:  {command}\n"
                f"  cwd:   {self.worktree}\n"
                f"  said:  {self.gate_output.strip()[-300:] or self.gate_result}\n"
                f"\n"
                f"  The gate runs inside the worktree, not the repo root, so a path"
                f" that only exists\n"
                f"  in the main checkout has to be absolute or listed under"
                f" [worktree] link.\n"
                f"\n"
                f"  This worktree holds no work -- no iteration ran -- so fixing the"
                f" gate and\n"
                f"  re-running is enough; the next attempt takes its own name. To"
                f" clear this one:\n"
                f"    git worktree remove {self.worktree} && git branch -D {self.branch}"
            )
        if self.gate_result.startswith("fail"):
            self.screen.log(
                f"  gate already fails on the base commit ({self.gate_result});"
                " that failure is the repository's, not this run's"
            )

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
            if completed.returncode == 0:
                self.gate_result = "pass"
            elif completed.returncode == 127:
                # 127 is the shell saying it could not find the command, which is
                # a broken gate, not broken code.  Kept out of the "fail" family
                # deliberately: `blocks_commit` keys off that prefix, and a gate
                # that cannot run must never be the reason an hour of work sits
                # uncommitted.
                self.gate_result = "misconfigured (rc=127: command not found)"
            else:
                self.gate_result = f"fail (rc={completed.returncode})"
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
        wider = self._wider_model() if self.last_outcome == "thrashing" else ""
        if wider:
            return (
                wider,
                agent.get("planner_thinking") or agent.get("thinking", ""),
                "retry",
            )
        return agent["model"], agent.get("thinking", ""), "editing"

    def _retry_step(self) -> str:
        """The step in play, if it is one that has already thrashed.

        Checked against the plan as it stands now rather than remembered: the
        agent may have split the step itself, or checked it off, and in either
        case the warning has done its job and should stop being shown.
        """
        step = self.rundir.current_step()
        return step if step and step in self.thrashed_steps else ""

    def _wider_model(self) -> str:
        """The configured model with the most room, when the last one ran out.

        Thrashing is the window losing to the codebase: the agent reads until it
        overflows, compacts, distrusts the summary and reads again.  Retrying the
        same step on the same model is retrying the thing that just failed for a
        reason that has not changed, so the retry goes to whichever model this
        project already names that measures widest -- on one-project that is
        90112 tokens of prompt budget against 49152, which is the difference
        between a file fitting and not.

        Only models the project configured are considered.  Picking a model the
        operator never named would be lmloop deciding what their hardware should
        load, and a wider window it has to swap in for is not obviously a better
        trade than a narrower one already resident.
        """
        agent = self.config["agent"]
        current = agent["model"]
        candidates = {name for name in (current, agent.get("planner_model")) if name}
        best, best_room = "", models.declared_window(current)
        if best_room is None:
            return ""  # unmeasured: no basis to call anything wider
        for name in candidates:
            room = models.declared_window(name)
            if room and room[0] > best_room[0]:
                best, best_room = name, room
        return best

    def iterate(self, number: int) -> None:
        base = self.rundir.base_commit
        self.model, thinking, role = self.roles()
        # Kept on the run so the status file can say which model is working and
        # under what settings.  `roles` can hand back a different model than the
        # one configured -- planning uses one, a thrash retry another -- and a
        # dashboard that shows the configured model is showing the wrong one.
        self.thinking, self.role = thinking, role
        self.window, self.max_output = models.declared_window(self.model) or (0, 0)
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
            history=previous_runs(self.worktree.parent, self.run_id),
            plan=self.rundir.read_plan(),
            plan_path=str(self.rundir.plan_path),
            plan_progress=self.rundir.plan_progress(),
            linked=getattr(self, "linked", []),
            interpreter=self.interpreter(),
            gate_command=self.config["gate"]["command"],
            gate_result=self.gate_result,
            gate_output=self.gate_output,
            gate_baseline=self.gate_baseline,
            defects=self.defects,
            thrashed_step=self._retry_step(),
            thrashed_times=self.thrashed_steps.get(self._retry_step(), 0),
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
        self._step_before = self.rundir.current_step()
        result = pi_runner.run(
            model=self.model,
            agent_name=self.config["agent"].get("harness", "pi"),
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
            # Only the *hard* stop reaches in here.  A plain STOP means "end the
            # run", and the boundary -- where the gate runs, the handoff is
            # written and the tree is committed -- is where ending it is worth
            # something; killing pi at minute 55 to save five minutes throws
            # away the handoff that made the hour reusable.  STOP-NOW, and a
            # SIGINT, say the iteration itself is the thing to end.
            should_stop=lambda: self.interrupted or self.rundir.stop_now_requested(),
            on_progress=lambda snap: self._show(number, snap),
        )

        # Which step defeated the window, and how often.  Recorded from the step
        # that was in play when the iteration started, not the one in play now:
        # a thrashing iteration writes nothing, so the plan has not moved, but
        # reading it after the fact would still be reading a file the agent was
        # free to edit mid-iteration.
        self.last_outcome = result.outcome
        if result.outcome == "thrashing" and self._step_before:
            self.thrashed_steps[self._step_before] = (
                self.thrashed_steps.get(self._step_before, 0) + 1
            )
            self.rundir.event(
                "step:thrashed",
                iteration=number,
                step=self._step_before[:160],
                times=self.thrashed_steps[self._step_before],
            )

        self.run_gate(number)
        # Structural checks run whatever the project configured, because the
        # damage an edit does is not project-specific -- see checks.py.
        # The plan is checked too, separately: it lives under `.lmloop/`, which
        # git excludes, so `checks.run` -- which works from the git diff -- can
        # never see the file that decides what every future iteration does.
        self.defects = checks.run(self.worktree, base) + self.rundir.plan_problems()
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
                carried=self.rundir.last_compaction_summary(
                number, self.config["agent"].get("harness", "pi")
            ),
            )
        summary = self._subject(number, result, handoff_written)

        commit = self.commit(number, summary, result, handoff_written, base)
        self.record(number, summary, result, handoff_written, commit, base)
        self.last_detail = result.detail or ""
        self.last_commit = commit

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
            (2, paint.dim(f"{snap['tokens_per_second']:.1f} tok/s") if snap["tokens_per_second"] else ""),
            (1, paint.dim(f"{snap['output_tokens']} out")),
            (7, paint.red(f"[{flags}]") if flags else ""),
        ])
        self.rundir.write_status({
            "run_id": self.run_id,
            "iteration": number,
            "max_iterations": self.max_iterations,
            "model": self.model,
            "thinking": self.thinking,
            "role": self.role,
            # Zero for a model nobody has measured; the display says "unmeasured"
            # rather than inventing a denominator.
            "context_window": self.window,
            "max_output_tokens": self.max_output,
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
            "input_tokens": snap["input_tokens"],
            "tokens_per_second": round(snap["tokens_per_second"], 2),
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
        self._plan_at_start = self.rundir.plan_progress()[0]
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
                continue
            transport = self._transport_failure()
            if transport:
                # The server, not the work.  Same backoff as a failed preflight,
                # and the iteration number is reused, so a restart of llama-swap
                # does not quietly cost one of twelve iterations.
                if not self._backoff(iteration, transport):
                    reason = f"model server unreachable: {transport}"
                    break
                iteration -= 1

        self.rundir.event(
            "run:complete",
            status=reason or "complete",
            iterations=iteration - 1,
            commitCount=gitops.commit_count(self.worktree, self.rundir.base_commit),
            worktreePath=str(self.worktree),
        )
        self.rundir.release()
        self.screen.close()
        self._summarise(reason, started)
        self._sweep()
        self._announce(reason, started, iteration - 1)
        return 0

    def _announce(self, reason: str | None, started: float, iterations: int) -> None:
        """Push one notification saying the run has stopped.

        Last, after the sweep, so the figures quoted are the ones that survive.
        Like the sweep it can never fail a run: a dead server, a typo in a URL
        and a network outage all end the same way, with an event in the log.
        """
        settings = self.config.get("notify", {})
        if not settings.get("url"):
            return

        failures: dict[str, int] = {}
        for event in self.rundir.read_events():
            if event.get("event") == "iteration:end":
                outcome = event.get("outcome", "")
                if outcome and outcome != "ok":
                    failures[outcome] = failures.get(outcome, 0) + 1

        try:
            import notify

            problem = notify.send(settings, {
                "repo": self.repo.name,
                "project": self.repo.name,
                "run_id": self.run_id,
                "objective": self.objective,
                "iterations": iterations,
                "commits": gitops.commit_count(self.worktree, self.rundir.base_commit),
                "hours": (time.monotonic() - started) / 3600,
                "plan": self.rundir.plan_progress(),
                "reason": reason or "complete",
                "failures": failures,
                "defects": self.defects,
            })
        except Exception as error:  # noqa: BLE001 - never fails a run
            problem = str(error)

        if problem:
            self.rundir.event("notify:failed", detail=problem)
            self.screen.log(f"  could not notify: {problem}")
        else:
            self.rundir.event("notify", topic=settings.get("topic", ""))

    def _sweep(self) -> None:
        """Reclaim the disk this run cost, now that it has stopped.

        At the end rather than on a timer: this is the moment the space appears
        and the moment somebody is watching, and one honest line about what was
        freed is easier to trust than a cron job quietly rewriting run
        directories overnight.  Nothing is lost -- streams are compressed and
        stay readable; only regenerable bytecode is removed.
        """
        settings = self.config.get("prune", {})
        if not settings.get("after_run", True):
            return
        try:
            import prune

            result = prune.prune(
                [self.repo],
                older_than_days=settings.get("older_than_days", 0.0),
                finished={self.run_id},
            )
        except Exception as error:  # noqa: BLE001 - housekeeping never fails a run
            self.rundir.event("prune:failed", detail=str(error))
            return
        freed = result["saved"] + result["bytecode"]
        if freed:
            self.rundir.event(
                "prune", files=len(result["files"]), saved=result["saved"],
                bytecode=result["bytecode"],
            )
            self.screen.log(f"  reclaimed {freed / 1e6:.0f} MB "
                            f"({len(result['files'])} streams compressed, bytecode dropped)")

    def _on_interrupt(self, *_args) -> None:
        if self.interrupted:
            raise KeyboardInterrupt
        self.interrupted = True
        # A signal is the operator asking for the terminal back, not for another
        # forty minutes of generation, so it cuts the iteration short rather than
        # waiting it out -- and says so, because the previous wording promised the
        # opposite of what the code did.  Nothing is lost either way: whatever the
        # iteration wrote is gated, checked and committed on the way out.
        self.screen.log("  stop requested; ending this iteration now and committing what it has")

    # What the agent says when the model server went away underneath it, rather
    # than when the model did something wrong.  pi retries these itself a few
    # times; these are the ones that outlast its retries, which means the server
    # was gone for minutes -- a restart, a reload, a swap -- not a blip.
    TRANSPORT = (
        "stream ended without finish_reason",
        "connection refused",
        "connection reset",
        "connection error",
        "remote end closed",
        "bad gateway",
        "service unavailable",
        "gateway timeout",
        # What pi actually reports when llama-swap stops answering mid-stream.
        # Observed: the server was shut down 23 minutes into an iteration and
        # the agent surfaced "Request timed out." -- which matched none of the
        # phrases above, so the loop recorded a genuine agent-error and charged
        # the run an iteration for a machine that was switched off.
        #
        # Safe as a bare phrase because of what it is tested against: lmloop's
        # own clocks produce the outcomes `timeout` and `stalled`, never
        # `agent-error`, so a timeout reported *inside* an agent-error is the
        # agent timing out on the model -- which is the transport, by
        # definition.
        "timed out",
        "no route to host",
        "name or service not known",
    )

    def _transport_failure(self) -> str:
        """The detail, if this iteration died of the server rather than itself.

        An iteration that ends this way has produced nothing and learned
        nothing, and charging it against `max_iterations` spends one of a very
        small number on an event that had nothing to do with the work.  Observed
        here: fifty minutes of generation ended by a llama-server being swapped
        for a faster build mid-stream.

        Only when it left no commit.  If the agent got far enough to change
        files, the iteration is worth keeping whatever killed it, and redoing it
        would mean redoing work that is already in git.
        """
        if self.last_outcome != "agent-error" or self.last_commit:
            return ""
        detail = (self.last_detail or "").lower()
        return self.last_detail if any(t in detail for t in self.TRANSPORT) else ""

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
