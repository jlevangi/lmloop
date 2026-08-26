"""The install has to keep describing the code it installs.

Packaging drift is silent in the worst way: a module left out of `py-modules`
imports perfectly from a clone and is simply absent once installed, so the
failure only ever reaches somebody who installed it properly.
"""

import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).parent
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())


class ModuleListingTests(unittest.TestCase):
    def listed(self):
        return set(PYPROJECT["tool"]["setuptools"]["py-modules"])

    def present(self):
        return {
            path.stem for path in ROOT.glob("*.py")
            if not path.name.startswith("test_") and path.name != "setup.py"
        }

    def test_every_module_in_the_repo_is_installed(self):
        missing = self.present() - self.listed()
        self.assertEqual(set(), missing,
                         f"modules that would vanish once installed: {sorted(missing)}")

    def test_nothing_is_listed_that_does_not_exist(self):
        extra = self.listed() - self.present()
        self.assertEqual(set(), extra, f"listed but absent: {sorted(extra)}")

    def test_tests_are_not_shipped(self):
        for name in self.listed():
            self.assertFalse(name.startswith("test_"), name)


class EntryPointTests(unittest.TestCase):
    def test_both_console_scripts_point_at_something_real(self):
        import lmloop
        for script, target in PYPROJECT["project"]["scripts"].items():
            module, _, attribute = target.partition(":")
            with self.subTest(script=script):
                self.assertEqual("lmloop", module)
                self.assertTrue(callable(getattr(lmloop, attribute, None)),
                                f"{target} is not callable")

    def test_the_runner_and_the_dashboard_both_have_one(self):
        self.assertIn("lmloop", PYPROJECT["project"]["scripts"])
        self.assertIn("lmloop-web", PYPROJECT["project"]["scripts"])


class DependencyTests(unittest.TestCase):
    def test_the_runner_needs_nothing(self):
        """Standard library only is what lets this be installed by cloning it."""
        self.assertEqual([], PYPROJECT["project"]["dependencies"])

    def test_the_dashboard_extras_are_the_two_it_actually_imports(self):
        extras = " ".join(PYPROJECT["project"]["optional-dependencies"]["web"]).lower()
        self.assertIn("pyjwt", extras)
        self.assertIn("requests", extras)

    def test_the_python_floor_matches_what_the_code_needs(self):
        """`tomllib` is 3.11; claiming less would install and then fail on
        the first config read."""
        self.assertEqual(">=3.11", PYPROJECT["project"]["requires-python"])


class ServiceUnitTests(unittest.TestCase):
    UNIT = (ROOT / "web" / "deploy" / "lmloop-web.service").read_text()

    def test_it_names_no_particular_person_or_checkout(self):
        for pattern in (r"/home/\w", r"%h/git/", r"/usr/bin/python3"):
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, self.UNIT),
                                  f"{pattern} is somebody's machine, not a default")

    def test_it_runs_the_installed_console_script(self):
        self.assertIn("ExecStart=%h/.local/bin/lmloop-web", self.UNIT)

    def test_it_still_pins_a_path_for_the_agent_and_git(self):
        """A manager-environment change would otherwise break run launching in
        a way that only surfaces hours later, inside a run."""
        self.assertIn("Environment=PATH=", self.UNIT)


class ExampleFileTests(unittest.TestCase):
    PERSONAL = re.compile(r"pierce|levangie|172\.20\.\d+|/home/\w")

    def test_no_shipped_example_carries_somebody_s_settings(self):
        import config
        examples = {
            "config.sample()": config.sample(),
            "model-budgets.example.json": (ROOT / "model-budgets.example.json").read_text(),
            "web.env.example": (ROOT / "web" / "deploy" / "web.env.example").read_text(),
        }
        for name, text in examples.items():
            with self.subTest(example=name):
                found = self.PERSONAL.findall(text)
                self.assertEqual([], found, f"{name} leaks {found}")


if __name__ == "__main__":
    unittest.main()
