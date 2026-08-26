"""What the dashboard's destructive operations actually do, before they move.

`archive`, `delete` and `open PR` are the three places the WebUI removes
something or reaches outside the machine. They are also the most carefully
written code in `web/server.py` -- archive copies, verifies by content hash,
publishes by rename, and rolls back if the worktree will not go -- and all of
that care is invisible to a refactor that only reads the method signatures.

These pin the behaviour, not the shape, so the lm-ka5.5 split can move the code
and be told if it changed what it does.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import runrecord
from web import runs as runs_module
from web import server


class Recording(server.Handler):
    """A Handler with the HTTP taken off.

    `BaseHTTPRequestHandler.__init__` wants a socket and answers a request; the
    methods under test only ever reply through `self.json`, so replacing that is
    enough to call them directly.
    """

    def __init__(self, config=None):        # noqa: D107 - deliberately not super()
        self.config = config or {"read_only": False, "python": "python3"}
        self.replies = []

    def json(self, payload, status=200):
        self.replies.append((status, payload))
        return payload

    @property
    def status(self):
        return self.replies[-1][0]

    @property
    def payload(self):
        return self.replies[-1][1]


def build_repo():
    """A repository with a worktree holding one run, as a real run leaves it."""
    root = Path(tempfile.mkdtemp())
    repo = root / "project"
    repo.mkdir()

    def git(*args, cwd=repo):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (repo / "seed.txt").write_text("seed\n")
    git("add", "-A")
    git("commit", "-qm", "seed")

    run_id = "2026-01-01-a-run-abc123"
    worktree = repo / ".worktrees" / run_id
    git("worktree", "add", "-q", "-b", f"lmloop/{run_id}", str(worktree))

    run_dir = worktree / ".lmloop" / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "prompt.md").write_text("do the thing\n")
    (run_dir / "plan.md").write_text("- [x] one\n- [ ] two\n")
    (run_dir / "notes.md").write_text("notes\n")
    (run_dir / "status.json").write_text(json.dumps({"phase": "stopped"}))
    (run_dir / "lmloop.log").write_text(
        json.dumps({"event": "run:start", "branch": f"lmloop/{run_id}"}) + "\n")
    (run_dir / "sessions").mkdir()
    (run_dir / "sessions" / "one.jsonl").write_text('{"a": 1}\n')

    archive = root / "archive"
    project = {"id": "project", "path": str(repo)}
    return root, repo, worktree, run_dir, project, archive


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        (self.root, self.repo, self.worktree, self.run_dir,
         self.project, self.archive) = build_repo()
        patch = mock.patch.object(runs_module, "ARCHIVE_ROOT", self.archive)
        patch.start()
        self.addCleanup(patch.stop)
        self.handler = Recording()

    def archive_run(self):
        return self.handler.archive_run(self.project, self.run_dir)

    # -- the guards, which all leave the worktree alone -------------------

    def test_a_run_with_a_live_loop_is_refused(self):
        with mock.patch.object(runs_module, "_holder", return_value=4242):
            self.archive_run()
        self.assertEqual(409, self.handler.status)
        self.assertIn("4242", self.handler.payload["error"])
        self.assertTrue(self.run_dir.is_dir(), "nothing removed")

    def test_an_already_archived_run_is_refused(self):
        with mock.patch.object(runs_module, "is_archived", return_value=True):
            self.archive_run()
        self.assertEqual(400, self.handler.status)

    def test_an_existing_archive_is_never_overwritten(self):
        target = self.archive / "project" / self.run_dir.name
        target.mkdir(parents=True)
        (target / "precious.md").write_text("an earlier archive\n")
        self.archive_run()
        self.assertEqual(409, self.handler.status)
        self.assertEqual("an earlier archive\n", (target / "precious.md").read_text())
        self.assertTrue(self.run_dir.is_dir(), "worktree left alone")

    def test_a_failed_copy_leaves_everything_alone(self):
        with mock.patch("shutil.copytree", side_effect=OSError("disk full")):
            self.archive_run()
        self.assertEqual(500, self.handler.status)
        self.assertTrue(self.run_dir.is_dir())
        self.assertFalse((self.archive / "project" / self.run_dir.name).exists())

    def test_a_copy_that_does_not_verify_is_thrown_away(self):
        """The verification guards the only deletion here -- the original run
        record. A stale target with the same number of files is not a copy."""
        import shutil
        real = shutil.copytree

        def lying_copy(*args, **kwargs):
            # `copytree` recurses through the module global, so this runs for
            # every subdirectory too -- hence signature-agnostic, and hence a
            # change to the *source* rather than the copy, which is idempotent.
            result = real(*args, **kwargs)
            (self.run_dir / "appeared-after-the-copy.md").write_text("x\n")
            return result

        with mock.patch("shutil.copytree", side_effect=lying_copy):
            self.archive_run()
        self.assertEqual(500, self.handler.status)
        self.assertIn("verification failed", self.handler.payload["error"])
        self.assertTrue(self.run_dir.is_dir(), "worktree left alone")
        self.assertFalse((self.archive / "project" / self.run_dir.name).exists())

    # -- the happy path ---------------------------------------------------

    def test_the_archive_is_a_byte_for_byte_copy(self):
        before = {
            str(p.relative_to(self.run_dir)): p.read_bytes()
            for p in self.run_dir.rglob("*") if p.is_file()
        }
        self.archive_run()
        target = self.archive / "project" / self.run_dir.name
        after = {
            str(p.relative_to(target)): p.read_bytes()
            for p in target.rglob("*") if p.is_file()
        }
        self.assertEqual(before, after)

    def test_nested_files_survive_the_copy(self):
        self.archive_run()
        target = self.archive / "project" / self.run_dir.name
        self.assertEqual('{"a": 1}\n', (target / "sessions" / "one.jsonl").read_text())

    def test_no_staging_directory_is_left_behind(self):
        self.archive_run()
        leftovers = [p.name for p in (self.archive / "project").iterdir()
                     if p.name.startswith(".")]
        self.assertEqual([], leftovers)

    def test_the_worktree_is_removed_once_the_copy_is_verified(self):
        self.archive_run()
        self.assertEqual(200, self.handler.status)
        self.assertFalse(self.worktree.exists())

    def test_the_record_is_restored_when_the_worktree_will_not_go(self):
        """Git refuses to remove a worktree with other files in it, and the
        run's record must not be the casualty of that refusal."""
        (self.worktree / "untracked-work.txt").write_text("the agent's work\n")
        self.archive_run()
        self.assertEqual(500, self.handler.status)
        self.assertTrue(self.worktree.exists(), "still there, as git said")
        self.assertTrue(self.run_dir.is_dir(), "and its record was put back")
        self.assertEqual("do the thing\n", (self.run_dir / "prompt.md").read_text())
        self.assertEqual("the agent's work\n",
                         (self.worktree / "untracked-work.txt").read_text())

    def test_the_archive_survives_a_failed_worktree_removal(self):
        """Archived is archived; the worktree is a separate question."""
        (self.worktree / "untracked-work.txt").write_text("work\n")
        self.archive_run()
        target = self.archive / "project" / self.run_dir.name
        self.assertTrue((target / "prompt.md").is_file())

    def test_only_lmloop_owned_links_are_removed_never_their_targets(self):
        real_venv = self.repo / ".venv"
        real_venv.mkdir()
        (real_venv / "marker").write_text("the project's own\n")
        (self.worktree / ".venv").symlink_to(real_venv)
        with mock.patch.object(server.config_module, "load", return_value={
            "worktree": {"link": [".venv"], "root": "{repo}/.worktrees/{run_id}",
                         "branch": "lmloop/{run_id}"},
        }):
            self.archive_run()
        self.assertTrue(real_venv.is_dir(), "the target is the project's, not ours")
        self.assertEqual("the project's own\n", (real_venv / "marker").read_text())

    def test_a_regular_file_at_a_link_name_is_left_alone(self):
        """Anything that is not a symlink at one of those names is user data."""
        (self.worktree / ".venv").write_text("not a link\n")
        with mock.patch.object(server.config_module, "load", return_value={
            "worktree": {"link": [".venv"], "root": "{repo}/.worktrees/{run_id}",
                         "branch": "lmloop/{run_id}"},
        }):
            self.archive_run()
        self.assertEqual("not a link\n", (self.worktree / ".venv").read_text())


class DeleteTests(unittest.TestCase):
    def setUp(self):
        (self.root, self.repo, self.worktree, self.run_dir,
         self.project, self.archive) = build_repo()
        patch = mock.patch.object(runs_module, "ARCHIVE_ROOT", self.archive)
        patch.start()
        self.addCleanup(patch.stop)
        self.handler = Recording()
        # Archiving removes the worktree, and delete only ever runs after it.
        # Git refuses to delete a branch still checked out in a worktree, so a
        # fixture that skipped this step would be testing a state that cannot
        # occur -- and would have said the branch could never be deleted.
        subprocess.run(["git", "worktree", "remove", "--force", str(self.worktree)],
                       cwd=self.repo, check=True, capture_output=True)
        # An archived copy, which is the only thing delete will touch.
        self.archived = self.archive / "project" / self.run_dir.name
        self.archived.mkdir(parents=True)
        (self.archived / "prompt.md").write_text("do the thing\n")
        (self.archived / "lmloop.log").write_text(json.dumps(
            {"event": "run:start", "branch": f"lmloop/{self.run_dir.name}"}) + "\n")

    def test_a_run_that_is_not_archived_cannot_be_deleted(self):
        """Losing the worktree and losing the record are two decisions.

        Its own directory, not the fixture's: archiving is what removes the
        worktree, so a run that has not been archived still has one.
        """
        live = Path(tempfile.mkdtemp()) / "runs" / "a-live-run"
        live.mkdir(parents=True)
        (live / "prompt.md").write_text("do the thing\n")
        self.handler.delete_run(self.project, live, {})
        self.assertEqual(400, self.handler.status)
        self.assertIn("archive this run before deleting", self.handler.payload["error"])
        self.assertTrue(live.is_dir(), "and nothing was removed")

    def test_an_archived_run_is_removed(self):
        self.handler.delete_run(self.project, self.archived, {})
        self.assertEqual(200, self.handler.status)
        self.assertFalse(self.archived.exists())

    def test_the_branch_is_kept_unless_asked_for(self):
        self.handler.delete_run(self.project, self.archived, {})
        self.assertIsNone(self.handler.payload["branch_deleted"])
        kept = subprocess.run(
            ["git", "rev-parse", "--verify", f"lmloop/{self.run_dir.name}"],
            cwd=self.repo, capture_output=True)
        self.assertEqual(0, kept.returncode, "branch still there")

    def test_the_branch_goes_when_it_is(self):
        self.handler.delete_run(self.project, self.archived, {"branch": True})
        self.assertEqual(f"lmloop/{self.run_dir.name}",
                         self.handler.payload["branch_deleted"])
        gone = subprocess.run(
            ["git", "rev-parse", "--verify", f"lmloop/{self.run_dir.name}"],
            cwd=self.repo, capture_output=True)
        self.assertNotEqual(0, gone.returncode)

    def test_the_branch_comes_from_the_run_not_from_its_name(self):
        """Guessing it as `lmloop/<run-id>` was a bug: it is only right while
        nobody sets `[worktree] branch`. See runrecord.resolved_branch."""
        (self.archived / "lmloop.log").write_text(json.dumps(
            {"event": "run:start", "branch": "custom/elsewhere"}) + "\n")
        with mock.patch.object(server.subprocess, "run") as ran:
            ran.return_value = mock.Mock(returncode=1)
            self.handler.delete_run(self.project, self.archived, {"branch": True})
        self.assertIn("custom/elsewhere", ran.call_args.args[0])

    def test_a_branch_that_will_not_delete_does_not_stop_the_removal(self):
        with mock.patch.object(server.subprocess, "run",
                               return_value=mock.Mock(returncode=1)):
            self.handler.delete_run(self.project, self.archived, {"branch": True})
        self.assertIsNone(self.handler.payload["branch_deleted"])
        self.assertFalse(self.archived.exists())


class OpenPrTests(unittest.TestCase):
    def setUp(self):
        (self.root, self.repo, self.worktree, self.run_dir,
         self.project, self.archive) = build_repo()
        self.handler = Recording()

    def test_a_missing_branch_is_a_404(self):
        (self.run_dir / "lmloop.log").write_text(json.dumps(
            {"event": "run:start", "branch": "lmloop/never-existed"}) + "\n")
        self.handler.open_pr(self.project, self.run_dir, {})
        self.assertEqual(404, self.handler.status)

    def test_a_branch_with_no_commits_beyond_base_is_refused(self):
        """Nothing to review, and `gh` would make an empty PR."""
        self.handler.open_pr(self.project, self.run_dir, {})
        self.assertEqual(400, self.handler.status)
        self.assertIn("no commits beyond", self.handler.payload["error"])

    def commit_on_branch(self):
        branch = f"lmloop/{self.run_dir.name}"
        (self.worktree / "work.txt").write_text("work\n")
        subprocess.run(["git", "add", "-A"], cwd=self.worktree, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-qm", "did work"], cwd=self.worktree,
                       check=True, capture_output=True)
        return branch

    def test_a_failed_push_never_reaches_the_forge(self):
        self.commit_on_branch()
        calls = []
        real = subprocess.run

        def fake(argv, **kwargs):
            calls.append(argv)
            if argv[:2] == ["git", "push"]:
                return mock.Mock(returncode=1, stderr="no remote", stdout="")
            return real(argv, **kwargs)

        with mock.patch.object(server.subprocess, "run", side_effect=fake):
            self.handler.open_pr(self.project, self.run_dir, {})
        self.assertEqual(500, self.handler.status)
        self.assertFalse([c for c in calls if c and c[0] == "gh"],
                         "gh must not run after a failed push")

    def test_an_existing_pull_request_is_an_answer_not_an_error(self):
        self.commit_on_branch()
        real = subprocess.run

        def fake(argv, **kwargs):
            if argv[:2] == ["git", "push"]:
                return mock.Mock(returncode=0, stdout="", stderr="")
            if argv[:3] == ["gh", "pr", "create"]:
                return mock.Mock(returncode=1, stdout="", stderr="already exists")
            if argv[:3] == ["gh", "pr", "view"]:
                return mock.Mock(returncode=0, stdout="https://example/pr/1\n", stderr="")
            return real(argv, **kwargs)

        with mock.patch.object(server.subprocess, "run", side_effect=fake):
            self.handler.open_pr(self.project, self.run_dir, {})
        self.assertEqual(200, self.handler.status)
        self.assertEqual("https://example/pr/1", self.handler.payload["url"])
        self.assertTrue(self.handler.payload["existing"])

    def test_the_title_defaults_to_the_objective_and_can_be_overridden(self):
        self.commit_on_branch()
        seen = {}
        real = subprocess.run

        def fake(argv, **kwargs):
            if argv[:2] == ["git", "push"]:
                return mock.Mock(returncode=0, stdout="", stderr="")
            if argv[:3] == ["gh", "pr", "create"]:
                seen["argv"] = argv
                return mock.Mock(returncode=0, stdout="https://example/pr/2\n", stderr="")
            return real(argv, **kwargs)

        with mock.patch.object(server.subprocess, "run", side_effect=fake):
            self.handler.open_pr(self.project, self.run_dir, {})
        self.assertIn("do the thing", seen["argv"])

        seen.clear()
        with mock.patch.object(server.subprocess, "run", side_effect=fake):
            self.handler.open_pr(self.project, self.run_dir, {"title": "mine"})
        self.assertIn("mine", seen["argv"])


class ControlTests(unittest.TestCase):
    """The five control actions that are an operation rather than a destination.

    Added after the lm-ka5.5 move found them uncovered: three mutations --
    starting a second loop beside a live one, `stop-now` no longer setting
    `STOP` as well, and `continue` leaving `PAUSE` behind -- all passed
    unnoticed. Each of those has a comment describing the incident it came
    from, and none of them had a test.
    """

    def setUp(self):
        (self.root, self.repo, self.worktree, self.run_dir,
         self.project, self.archive) = build_repo()
        self.handler = Recording({"read_only": False, "python": "python3"})

    def control(self, action, payload=None):
        return self.handler.control(self.project, self.run_dir, action, payload or {})

    def sentinels(self):
        return {name for name in ("PAUSE", "STOP", "STOP-NOW")
                if (self.run_dir / name).exists()}

    def test_pause_and_resume_are_one_file(self):
        self.control("pause")
        self.assertEqual({"PAUSE"}, self.sentinels())
        self.control("resume")
        self.assertEqual(set(), self.sentinels())

    def test_stop_sets_only_stop(self):
        self.control("stop")
        self.assertEqual({"STOP"}, self.sentinels())

    def test_stop_now_sets_both_sentinels(self):
        """Every reader that only knows about STOP still has to see a run that
        is stopping -- see rundir.stop_now_requested."""
        self.control("stop-now")
        self.assertEqual({"STOP", "STOP-NOW"}, self.sentinels())

    def test_resuming_a_run_that_was_never_paused_is_not_an_error(self):
        self.control("resume")
        self.assertEqual(200, self.handler.status)

    def test_an_unknown_action_is_refused_by_name(self):
        self.control("demolish")
        self.assertEqual(400, self.handler.status)
        self.assertIn("demolish", self.handler.payload["error"])

    def test_continue_refuses_to_start_a_second_loop(self):
        """Two loops in one worktree commit over each other and write the same
        status file. Refused here rather than by the child, whose complaint
        goes to a pipe nobody reads."""
        with mock.patch.object(runs_module, "_holder", return_value=9999):
            self.control("continue")
        self.assertEqual(409, self.handler.status)
        self.assertIn("9999", self.handler.payload["error"])

    def test_continue_does_not_launch_anything_when_it_refuses(self):
        with mock.patch.object(runs_module, "_holder", return_value=9999), \
             mock.patch.object(server.service.subprocess, "Popen") as popen:
            self.control("continue")
        popen.assert_not_called()

    def test_continue_clears_every_sentinel_including_pause(self):
        """A run holding on PAUSE is just as stopped as one that finished.
        Leaving that behind spawned a second loop that went straight back into
        the hold, so the button did nothing and said nothing about why."""
        for name in ("PAUSE", "STOP", "STOP-NOW"):
            (self.run_dir / name).touch()
        with mock.patch.object(runs_module, "_holder", return_value=0), \
             mock.patch.object(server.service.subprocess, "Popen") as popen:
            popen.return_value.wait.return_value = 0
            self.control("continue")
        self.assertEqual(set(), self.sentinels())

    def test_continue_passes_the_run_and_the_iteration_count(self):
        with mock.patch.object(runs_module, "_holder", return_value=0), \
             mock.patch.object(server.service.subprocess, "Popen") as popen:
            popen.return_value.wait.return_value = 0
            self.control("continue", {"iterations": 7})
        argv = popen.call_args.args[0]
        self.assertIn("resume", argv)
        self.assertIn(self.run_dir.name, argv)
        self.assertIn("7", argv)

    def test_continue_forwards_a_model_and_thinking_override(self):
        with mock.patch.object(runs_module, "_holder", return_value=0), \
             mock.patch.object(server.service.subprocess, "Popen") as popen:
            popen.return_value.wait.return_value = 0
            self.control("continue", {"model": "p/m", "thinking": "low"})
        argv = popen.call_args.args[0]
        self.assertIn("--model", argv)
        self.assertIn("p/m", argv)
        self.assertIn("--thinking", argv)

    def test_a_child_that_dies_immediately_reports_why(self):
        """Throwing the reason away is what made a dead button
        indistinguishable from a working one."""
        def die(argv, **kwargs):
            kwargs["stdout"].write(b"lmloop: no model set\n")
            return mock.Mock(wait=mock.Mock(return_value=1))

        with mock.patch.object(runs_module, "_holder", return_value=0), \
             mock.patch.object(server.service.subprocess, "Popen", side_effect=die):
            self.control("continue")
        self.assertEqual(500, self.handler.status)
        self.assertIn("no model set", self.handler.payload["error"])

    def test_a_child_still_running_after_a_moment_counts_as_started(self):
        with mock.patch.object(runs_module, "_holder", return_value=0), \
             mock.patch.object(server.service.subprocess, "Popen") as popen:
            popen.return_value.wait.side_effect = \
                server.service.subprocess.TimeoutExpired("lmloop", 1.5)
            self.control("continue")
        self.assertEqual(200, self.handler.status)

    def test_archive_delete_and_pr_are_dispatched_not_handled_here(self):
        """They are a destination rather than an operation; routing is what
        the handler is for."""
        for action, method in (("archive", "archive_run"),
                               ("delete", "delete_run"),
                               ("pr", "open_pr")):
            with self.subTest(action=action):
                with mock.patch.object(Recording, method) as called:
                    self.control(action)
                called.assert_called_once()


class CreateProjectTests(unittest.TestCase):
    """The only untrusted path component in the system.

    A project name arrives from a browser and becomes a directory. It is
    matched against a strict pattern rather than sanitised, because rejecting a
    bad name is always right and guessing what somebody meant by `../` never
    is. None of that was covered until the lm-ka5.5 split went looking.
    """

    def setUp(self):
        self.roots = Path(tempfile.mkdtemp())
        self.handler = Recording({
            "roots": [self.roots], "read_only": False, "python": "python3",
        })

    def create(self, name, objective=""):
        return self.handler.create_project({"name": name, "objective": objective})

    def test_an_ordinary_name_is_created_as_a_git_repository(self):
        self.create("my-project", "do the thing")
        self.assertEqual(200, self.handler.status)
        target = self.roots / "my-project"
        self.assertTrue((target / ".git").is_dir())
        self.assertIn("do the thing", (target / "README.md").read_text())

    def test_it_starts_with_a_commit_so_a_worktree_can_branch_off_it(self):
        """A repository with no commits has no HEAD, and a worktree cannot be
        branched off nothing."""
        self.create("my-project")
        head = subprocess.run(["git", "rev-parse", "HEAD"],
                              cwd=self.roots / "my-project", capture_output=True)
        self.assertEqual(0, head.returncode)

    def test_a_name_that_climbs_out_of_the_root_is_refused(self):
        """Checked by what was created, not by probing outside the root --
        anything may already exist out there, and a test that looks would be
        asserting about the machine rather than about the guard."""
        for name in ("../escape", "../../etc", "a/../../b", "/absolute"):
            with self.subTest(name=name):
                before = sorted(path.name for path in self.roots.iterdir())
                self.create(name)
                self.assertEqual(400, self.handler.status)
                self.assertEqual(before,
                                 sorted(path.name for path in self.roots.iterdir()))

    def test_a_symlinked_name_that_resolves_outside_the_root_is_refused(self):
        """The only way to reach the root-escape guard.

        No name that satisfies the pattern can climb out with `../` -- it has
        to start alphanumeric and cannot contain a separator -- so the guard
        looks redundant until you notice `resolve()` follows symlinks. An entry
        inside the root pointing outside it is the case it defends, and the one
        a `../` test can never reach.
        """
        outside = Path(tempfile.mkdtemp()) / "elsewhere"
        outside.mkdir()
        (self.roots / "innocent").symlink_to(outside)
        self.create("innocent")
        self.assertEqual(400, self.handler.status)
        self.assertIn("escapes", self.handler.payload["error"])
        self.assertEqual([], list(outside.iterdir()), "nothing written through it")

    def test_a_name_with_a_separator_is_refused(self):
        for name in ("a/b", "a\\b"):
            with self.subTest(name=name):
                self.create(name)
                self.assertEqual(400, self.handler.status)

    def test_a_dotfile_name_is_refused(self):
        for name in (".", "..", ".hidden", ".git"):
            with self.subTest(name=name):
                self.create(name)
                self.assertEqual(400, self.handler.status)

    def test_an_empty_or_overlong_name_is_refused(self):
        self.create("")
        self.assertEqual(400, self.handler.status)
        self.create("x" * 65)
        self.assertEqual(400, self.handler.status)

    def test_a_name_of_exactly_the_limit_is_allowed(self):
        self.create("x" * 64)
        self.assertEqual(200, self.handler.status)

    def test_an_existing_project_is_never_overwritten(self):
        (self.roots / "taken").mkdir()
        (self.roots / "taken" / "precious.txt").write_text("somebody's work\n")
        self.create("taken")
        self.assertEqual(409, self.handler.status)
        self.assertEqual("somebody's work\n",
                         (self.roots / "taken" / "precious.txt").read_text())

    def test_nothing_is_created_when_the_name_is_refused(self):
        before = sorted(p.name for p in self.roots.iterdir())
        self.create("../escape")
        self.assertEqual(before, sorted(p.name for p in self.roots.iterdir()))


class StartRunTests(unittest.TestCase):
    """Launching a run from the dashboard. Also uncovered until now."""

    def setUp(self):
        self.roots = Path(tempfile.mkdtemp())
        (self.roots / "project").mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.roots / "project",
                       check=True, capture_output=True)
        self.handler = Recording({
            "roots": [self.roots], "read_only": False, "python": "python3",
            "default_model": "", "default_thinking": "low",
            "default_max_iterations": 20,
        })

    def start(self, payload, returncode=0, stdout="run-id-here"):
        with mock.patch.object(server.subprocess, "run") as ran:
            ran.return_value = mock.Mock(returncode=returncode, stdout=stdout, stderr="")
            self.handler.start_run(payload)
        return ran

    def test_an_objective_is_required(self):
        ran = self.start({"project": "project", "objective": "   "})
        self.assertEqual(400, self.handler.status)
        ran.assert_not_called()

    def test_an_unknown_project_is_refused_without_launching(self):
        ran = self.start({"project": "nonesuch", "objective": "do it"})
        self.assertEqual(400, self.handler.status)
        ran.assert_not_called()

    def test_a_run_is_started_detached(self):
        """The dashboard cannot hold a process for hours, so the run has to
        outlive the request that asked for it."""
        ran = self.start({"project": "project", "objective": "do it"})
        argv = ran.call_args.args[0]
        self.assertIn("run", argv)
        self.assertIn("do it", argv)
        self.assertIn("--detach", argv)

    def test_the_configured_defaults_are_applied(self):
        ran = self.start({"project": "project", "objective": "do it"})
        argv = ran.call_args.args[0]
        self.assertIn("--thinking", argv)
        self.assertIn("low", argv)
        self.assertIn("20", argv)

    def test_the_payload_overrides_the_defaults(self):
        ran = self.start({"project": "project", "objective": "do it",
                          "model": "p/m", "thinking": "high", "max_iterations": 3})
        argv = ran.call_args.args[0]
        self.assertIn("p/m", argv)
        self.assertIn("high", argv)
        self.assertIn("3", argv)

    def test_an_empty_default_model_adds_no_flag(self):
        """There is no shipped default model any more; the dashboard must not
        invent one -- see config.require_model."""
        ran = self.start({"project": "project", "objective": "do it"})
        self.assertNotIn("--model", ran.call_args.args[0])

    def test_a_launch_that_fails_reports_why(self):
        self.start({"project": "project", "objective": "do it"},
                   returncode=1, stdout="lmloop: no model set")
        self.assertEqual(500, self.handler.status)
        self.assertIn("no model set", self.handler.payload["error"])


if __name__ == "__main__":
    unittest.main()
