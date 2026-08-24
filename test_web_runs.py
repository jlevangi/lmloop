import json
import tempfile
import unittest
from pathlib import Path

from web import runs


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

    def test_latest_start_event_controls_agent_label(self):
        run_dir = self.make_run(self.project, "resumed", agent="pi")
        with (run_dir / "lmloop.log").open("a") as log:
            log.write(json.dumps({"event": "run:start", "agent": "omp"}) + "\n")
        summary = runs.summarise({"id": "project", "path": str(self.project)}, run_dir)
        self.assertEqual("omp", summary["agent"])


if __name__ == "__main__":
    unittest.main()