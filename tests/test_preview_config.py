import tempfile
import unittest
from pathlib import Path

import config
from config_defaults import DEFAULTS
from web.service import create_project


class PreviewConfigTests(unittest.TestCase):
    def problems(self, value):
        return config.validate({"preview": value}, Path(".lmloop.toml"))

    def test_default_preview_is_disabled(self):
        self.assertEqual([], DEFAULTS["preview"]["command"])

    def test_command_must_be_argv_list_of_strings(self):
        self.assertTrue(self.problems({"command": "python3 -m http.server"}))
        self.assertTrue(self.problems({"command": ["python3", 3]}))
        self.assertEqual([], self.problems({"command": []}))

    def test_preview_bounds_and_paths(self):
        self.assertTrue(self.problems({"port": 0}))
        self.assertTrue(self.problems({"port": 65536}))
        self.assertTrue(self.problems({"port": True}))
        self.assertTrue(self.problems({"startup_timeout_seconds": 0}))
        self.assertTrue(self.problems({"startup_timeout_seconds": 3601}))
        self.assertTrue(self.problems({"path": "app"}))
        self.assertTrue(self.problems({"ready_path": "ready"}))
        self.assertEqual([], self.problems({
            "port": 65535, "path": "/app", "ready_path": "/health",
            "startup_timeout_seconds": 3600,
        }))

    def test_url_allows_preview_placeholders(self):
        self.assertEqual([], self.problems({
            "url": "http://{browser-host}:{port}{path}",
        }))

    def test_static_web_project_has_stdlib_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status, reply = create_project(
                {"name": "site", "objective": "make a site"},
                {"roots": [root]},
            )
            self.assertEqual(200, status)
            target = root / "site"
            self.assertTrue((target / "index.html").is_file())
            text = (target / ".lmloop.toml").read_text()
            self.assertIn('command = ["python3", "-m", "http.server", "{port}"]', text)
            self.assertIn("static-web", text)

    def test_unknown_project_template_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            status, reply = create_project(
                {"name": "site", "template": "react"},
                {"roots": [Path(directory)]},
            )
            self.assertEqual(400, status)
            self.assertIn("template", reply["error"])


if __name__ == "__main__":
    unittest.main()
