import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from web import runs
from web.server import Handler


class NestedPilotDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.project = Path(tempfile.mkdtemp()) / "project"
        self.project.mkdir()

    @staticmethod
    def make_run(base: Path, run_id: str, agent: str = "omp") -> Path:
        run_dir = base / ".worktrees" / run_id / ".lmloop" / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "status.json").write_text(json.dumps({
            "phase": "stopped", "model": "local/model", "updated_at": "",
        }))
        (run_dir / "lmloop.log").write_text(json.dumps({
            "event": "run:start", "agent": agent,
        }) + "\n")
        return run_dir

    def test_nested_pilot_base_is_discovered(self):
        normal = self.make_run(self.project, "normal")
        pilot = self.make_run(self.project / ".pilot-bases" / "pinned", "pilot")
        self.assertEqual({normal, pilot}, set(runs.run_dirs(self.project)))

    def test_summary_surfaces_the_agent_from_run_start(self):
        run_dir = self.make_run(self.project, "pilot", agent="omp")
        summary = runs.summarise(
            {"id": "project", "path": str(self.project)}, run_dir
        )
        self.assertEqual("omp", summary["agent"])

    def test_nested_run_has_owning_base_and_distinct_route(self):
        base = self.project / ".pilot-bases" / "pinned"
        run_dir = self.make_run(base, "same")
        self.make_run(self.project, "same", agent="pi")
        self.assertEqual(base, runs.owner(self.project, run_dir))
        self.assertEqual("pinned::same", runs.route_id(self.project, run_dir))

    def test_follow_on_run_under_a_worktree_is_discovered_and_owned(self):
        parent = self.project / ".worktrees" / "first-run"
        child = self.make_run(parent, "follow-on")
        self.assertIn(child, runs.run_dirs(self.project))
        self.assertEqual(parent, runs.owner(self.project, child))
        self.assertEqual("first-run::follow-on", runs.route_id(self.project, child))

    def test_latest_start_event_controls_agent_label(self):
        run_dir = self.make_run(self.project, "resumed", agent="pi")
        with (run_dir / "lmloop.log").open("a") as log:
            log.write(json.dumps({"event": "run:start", "agent": "omp"}) + "\n")
        summary = runs.summarise({"id": "project", "path": str(self.project)}, run_dir)
        self.assertEqual("omp", summary["agent"])

    def test_running_summary_calculates_eta_from_durable_events(self):
        run_dir = self.make_run(self.project, "eta")
        (run_dir / "plan.md").write_text("- [x] one\n- [x] two\n- [ ] three\n")
        (run_dir / "status.json").write_text(json.dumps({
            "phase": "working", "iteration": 3, "max_iterations": 6,
            "elapsed_seconds": 60, "updated_at": datetime.now(timezone.utc).isoformat(),
        }))
        with (run_dir / "lmloop.log").open("a") as log:
            log.write(json.dumps({"event": "iteration:end", "elapsedMs": 600000,
                                  "planDone": 1, "planTotal": 3}) + "\n")
            log.write(json.dumps({"event": "iteration:end", "elapsedMs": 1200000,
                                  "planDone": 2, "planTotal": 3}) + "\n")
        summary = runs.summarise({"id": "project", "path": str(self.project)}, run_dir)
        self.assertEqual("running", summary["state"])
        self.assertEqual(840, summary["eta_seconds"])
        self.assertEqual("plan steps", summary["eta_basis"])


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.project = self.root / "project"
        self.project.mkdir()
        self.run_id = "finished-run"
        self.run_dir = (
            self.project / ".worktrees" / self.run_id
            / ".lmloop" / "runs" / self.run_id
        )
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "prompt.md").write_text("Keep this history\n")
        (self.run_dir / "status.json").write_text(json.dumps({
            "phase": "completed", "updated_at": datetime.now(timezone.utc).isoformat(),
        }))
        (self.run_dir / "lmloop.log").write_text(json.dumps({
            "event": "run:complete", "commitCount": 2,
        }) + "\n")
        self.archive = self.root / "archive"

    def test_archived_run_remains_discoverable_and_read_only(self):
        target = self.archive / "project" / self.run_id
        target.mkdir(parents=True)
        for source in self.run_dir.iterdir():
            (target / source.name).write_bytes(source.read_bytes())

        with mock.patch.object(runs, "ARCHIVE_ROOT", self.archive):
            summary = runs.summarise(
                {"id": "project", "path": str(self.project)}, target
            )
            self.assertEqual([target], runs.archived_dirs("project"))
            self.assertEqual("archived", summary["state"])
            self.assertTrue(summary["archived"])
            self.assertEqual("Keep this history", summary["objective"])
            self.assertEqual(2, summary["commits"])
            self.assertEqual("", runs.detail(
                {"id": "project", "path": str(self.project)}, target
            )["worktree"])

    def test_archive_never_forces_worktree_removal(self):
        handler = Handler.__new__(Handler)
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        (self.run_dir.parents[2] / ".pi").mkdir()
        (self.run_dir.parents[2] / ".pi" / "settings.json").write_text("generated")
        (self.project / ".venv").mkdir()
        (self.run_dir.parents[2] / ".venv").symlink_to(self.project / ".venv")

        with mock.patch.object(runs, "ARCHIVE_ROOT", self.archive), \
             mock.patch("web.server.subprocess.run", return_value=completed) as run, \
             mock.patch.object(Handler, "json") as reply:
            handler.archive_run(
                {"id": "project", "path": str(self.project)}, self.run_dir
            )

        payload = reply.call_args.args[0]
        self.assertTrue(payload["archived"])
        argv = run.call_args.args[0]
        self.assertEqual(["git", "worktree", "remove", str(self.run_dir.parents[2])], argv)
        self.assertNotIn("--force", argv)
        self.assertFalse((self.run_dir.parents[2] / ".pi" / "settings.json").exists())
        self.assertFalse((self.run_dir.parents[2] / ".venv").exists())

    def test_existing_archive_never_merges_or_removes_source(self):
        target = self.archive / "project" / self.run_id
        target.mkdir(parents=True)
        (target / "stale").write_text("older record")
        handler = Handler.__new__(Handler)

        with mock.patch.object(runs, "ARCHIVE_ROOT", self.archive), \
             mock.patch.object(Handler, "json") as reply, \
             mock.patch("web.server.subprocess.run") as run:
            handler.archive_run(
                {"id": "project", "path": str(self.project)}, self.run_dir
            )

        self.assertEqual(409, reply.call_args.args[1])
        self.assertTrue(self.run_dir.is_dir())
        self.assertEqual("older record", (target / "stale").read_text())
        run.assert_not_called()

    def test_failed_copy_leaves_no_archive_and_retry_succeeds(self):
        import shutil

        handler = Handler.__new__(Handler)
        real_copytree = shutil.copytree
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(runs, "ARCHIVE_ROOT", self.archive), \
             mock.patch.object(Handler, "json") as reply, \
             mock.patch("shutil.copytree", side_effect=OSError("disk full")):
            handler.archive_run(
                {"id": "project", "path": str(self.project)}, self.run_dir
            )

        target = self.archive / "project" / self.run_id
        self.assertEqual(500, reply.call_args.args[1])
        self.assertFalse(target.exists())
        self.assertEqual([], list(target.parent.glob(f".{self.run_id}.*")))

        with mock.patch.object(runs, "ARCHIVE_ROOT", self.archive), \
             mock.patch.object(Handler, "json") as reply, \
             mock.patch("shutil.copytree", side_effect=real_copytree), \
             mock.patch("web.server.subprocess.run", return_value=completed):
            handler.archive_run(
                {"id": "project", "path": str(self.project)}, self.run_dir
            )

        self.assertTrue(reply.call_args.args[0]["archived"])
        self.assertTrue((target / "prompt.md").is_file())

    def test_failed_non_forced_removal_keeps_verified_archive(self):
        handler = Handler.__new__(Handler)
        refused = SimpleNamespace(returncode=1, stdout="", stderr="contains modified files")

        with mock.patch.object(runs, "ARCHIVE_ROOT", self.archive), \
             mock.patch("web.server.subprocess.run", return_value=refused) as run, \
             mock.patch.object(Handler, "json") as reply:
            handler.archive_run(
                {"id": "project", "path": str(self.project)}, self.run_dir
            )

        self.assertEqual(500, reply.call_args.args[1])
        self.assertIn("source record was restored", reply.call_args.args[0]["error"])
        self.assertTrue((self.archive / "project" / self.run_id / "prompt.md").is_file())
        self.assertTrue((self.run_dir / "prompt.md").is_file())
        self.assertNotIn("--force", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
