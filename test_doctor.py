import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
import doctor
import harness
import models
import runrecord


class StatusTests(unittest.TestCase):
    def test_a_fail_beats_a_warn_beats_ok(self):
        self.assertEqual(doctor.FAIL, doctor.worst(
            [("a", doctor.OK, ""), ("b", doctor.WARN, ""), ("c", doctor.FAIL, "")]))
        self.assertEqual(doctor.WARN, doctor.worst(
            [("a", doctor.OK, ""), ("b", doctor.WARN, "")]))
        self.assertEqual(doctor.OK, doctor.worst([("a", doctor.OK, "")]))

    def test_nothing_at_all_is_ok(self):
        self.assertEqual(doctor.OK, doctor.worst([]))


class HarnessCheckTests(unittest.TestCase):
    def test_an_installed_agent_passes_and_says_where(self):
        with mock.patch.object(doctor.shutil, "which", return_value="/usr/bin/pi"):
            name, status, detail = doctor.harness_check(harness, {"agent": {"harness": "pi"}})
        self.assertEqual(doctor.OK, status)
        self.assertIn("/usr/bin/pi", detail)

    def test_a_missing_binary_is_a_failure_naming_it(self):
        with mock.patch.object(doctor.shutil, "which", return_value=None):
            _, status, detail = doctor.harness_check(harness, {"agent": {"harness": "omp"}})
        self.assertEqual(doctor.FAIL, status)
        self.assertIn("omp", detail)
        self.assertIn("not on PATH", detail)

    def test_an_agent_nobody_has_heard_of_is_a_failure(self):
        _, status, detail = doctor.harness_check(harness, {"agent": {"harness": "nonesuch"}})
        self.assertEqual(doctor.FAIL, status)
        self.assertIn("unknown harness", detail)


class ModelCheckTests(unittest.TestCase):
    def test_no_model_is_a_failure_that_says_how_to_find_one(self):
        _, status, detail = doctor.model_check(models, {"agent": {"model": ""}})
        self.assertEqual(doctor.FAIL, status)
        self.assertIn("lmloop models", detail)

    def test_a_known_model_reports_its_split(self):
        with mock.patch.object(models, "declared_window", return_value=(100, 10)):
            _, status, detail = doctor.model_check(
                models, {"agent": {"model": "p/m", "harness": "pi"}})
        self.assertEqual(doctor.OK, status)
        self.assertIn("100 context + 10 output", detail)

    def test_an_unmeasured_model_warns_rather_than_fails(self):
        """A run works without a window; everything that reasons about room
        is blind, which is worth saying and not worth refusing over."""
        with mock.patch.object(models, "declared_window", return_value=None):
            _, status, _ = doctor.model_check(
                models, {"agent": {"model": "p/m", "harness": "pi"}})
        self.assertEqual(doctor.WARN, status)


class ServerCheckTests(unittest.TestCase):
    CONFIG = {"agent": {"model": "llama-swap/m"},
              "models": {"llama_swap_url": "http://server:1"}}

    def test_a_remote_model_needs_no_server(self):
        with mock.patch.object(models, "is_local", return_value=False), \
             mock.patch.object(models, "preflight") as preflight:
            _, status, _ = doctor.server_check(models, self.CONFIG)
        self.assertEqual(doctor.OK, status)
        preflight.assert_not_called()

    def test_an_unreachable_server_is_a_failure(self):
        with mock.patch.object(models, "is_local", return_value=True), \
             mock.patch.object(models, "preflight", return_value=(False, "unreachable")):
            _, status, detail = doctor.server_check(models, self.CONFIG)
        self.assertEqual(doctor.FAIL, status)
        self.assertIn("unreachable", detail)

    def test_a_model_the_server_has_never_heard_of_is_a_failure(self):
        """`preflight` answers "will this load", which for an unknown name is
        an optimistic yes -- it reports the swap it would attempt."""
        with mock.patch.object(models, "is_local", return_value=True), \
             mock.patch.object(models, "local_name", return_value="m"), \
             mock.patch.object(models, "preflight", return_value=(True, "will load")), \
             mock.patch.object(models, "available", return_value=["other", "another"]):
            _, status, detail = doctor.server_check(models, self.CONFIG)
        self.assertEqual(doctor.FAIL, status)
        self.assertIn("no model named `m`", detail)
        self.assertIn("other", detail, "say what it does serve")

    def test_a_model_the_server_does_serve_passes(self):
        with mock.patch.object(models, "is_local", return_value=True), \
             mock.patch.object(models, "local_name", return_value="m"), \
             mock.patch.object(models, "preflight", return_value=(True, "already loaded")), \
             mock.patch.object(models, "available", return_value=["m", "other"]):
            _, status, _ = doctor.server_check(models, self.CONFIG)
        self.assertEqual(doctor.OK, status)

    def test_a_catalogue_that_cannot_be_read_does_not_break_the_check(self):
        """A diagnostic must not add a traceback of its own."""
        with mock.patch.object(models, "is_local", return_value=True), \
             mock.patch.object(models, "local_name", return_value="m"), \
             mock.patch.object(models, "preflight", return_value=(True, "fine")), \
             mock.patch.object(models, "available", side_effect=OSError("refused")):
            _, status, _ = doctor.server_check(models, self.CONFIG)
        self.assertEqual(doctor.OK, status)


class GateCheckTests(unittest.TestCase):
    def test_no_gate_is_fine(self):
        _, status, _ = doctor.gate_check({"gate": {"command": ""}}, Path("/tmp"))
        self.assertEqual(doctor.OK, status)

    def test_a_gate_on_path_passes(self):
        with mock.patch.object(doctor.shutil, "which", return_value="/usr/bin/make"):
            _, status, _ = doctor.gate_check({"gate": {"command": "make test"}}, Path("/tmp"))
        self.assertEqual(doctor.OK, status)

    def test_a_gate_that_is_a_tracked_script_passes(self):
        """The gate runs with cwd = the worktree, so a tracked path is fine."""
        repo = Path(tempfile.mkdtemp())
        (repo / "check.sh").write_text("#!/bin/sh\n")
        with mock.patch.object(doctor.shutil, "which", return_value=None):
            _, status, _ = doctor.gate_check({"gate": {"command": "check.sh"}}, repo)
        self.assertEqual(doctor.OK, status)

    def test_a_gate_that_is_neither_warns(self):
        with mock.patch.object(doctor.shutil, "which", return_value=None):
            _, status, detail = doctor.gate_check(
                {"gate": {"command": "nonesuch --check"}}, Path(tempfile.mkdtemp()))
        self.assertEqual(doctor.WARN, status)
        self.assertIn("nonesuch", detail)


class NotifyCheckTests(unittest.TestCase):
    def test_no_url_is_disabled_not_broken(self):
        _, status, detail = doctor.notify_check(config, {"notify": {"url": ""}})
        self.assertEqual(doctor.OK, status)
        self.assertEqual("disabled", detail)

    def test_a_token_reference_that_resolves_to_nothing_warns(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            _, status, detail = doctor.notify_check(
                config, {"notify": {"url": "https://n", "token": "env:NOPE"}})
        self.assertEqual(doctor.WARN, status)
        self.assertIn("env:NOPE", detail)

    def test_a_token_that_resolves_is_fine(self):
        with mock.patch.dict(os.environ, {"TOK": "value"}):
            _, status, _ = doctor.notify_check(
                config, {"notify": {"url": "https://n", "token": "env:TOK"}})
        self.assertEqual(doctor.OK, status)

    def test_no_token_at_all_is_fine(self):
        _, status, _ = doctor.notify_check(config, {"notify": {"url": "https://n"}})
        self.assertEqual(doctor.OK, status)


class ConfigCheckTests(unittest.TestCase):
    def test_a_clean_config_passes_and_names_the_files(self):
        repo = Path(tempfile.mkdtemp())
        (repo / ".lmloop.toml").write_text('[agent]\nmodel = "p/m"\n')
        with mock.patch.object(config, "GLOBAL_CONFIG", repo / "absent.toml"):
            _, status, detail = doctor.config_check(config, repo)
        self.assertEqual(doctor.OK, status)
        self.assertIn(".lmloop.toml", detail)

    def test_a_typo_is_a_failure_carrying_the_suggestion(self):
        repo = Path(tempfile.mkdtemp())
        (repo / ".lmloop.toml").write_text('[agent]\nmodle = "p/m"\n')
        with mock.patch.object(config, "GLOBAL_CONFIG", repo / "absent.toml"):
            _, status, detail = doctor.config_check(config, repo)
        self.assertEqual(doctor.FAIL, status)
        self.assertIn("did you mean `model`?", detail)

    def test_no_config_at_all_is_defaults_not_an_error(self):
        repo = Path(tempfile.mkdtemp())
        with mock.patch.object(config, "GLOBAL_CONFIG", repo / "absent.toml"):
            _, status, detail = doctor.config_check(config, repo)
        self.assertEqual(doctor.OK, status)
        self.assertIn("defaults", detail)


class WholeRunTests(unittest.TestCase):
    def test_every_check_returns_a_name_status_and_detail(self):
        repo = Path(tempfile.mkdtemp())
        cfg = config.load(repo)
        with mock.patch.object(models, "is_local", return_value=False):
            results = doctor.check(repo, cfg, (config, harness, models, runrecord))
        self.assertTrue(results)
        for entry in results:
            self.assertEqual(3, len(entry))
            name, status, detail = entry
            self.assertIsInstance(name, str)
            self.assertIn(status, (doctor.OK, doctor.WARN, doctor.FAIL))
            self.assertIsInstance(detail, str)

    def test_no_check_raises_on_a_hostile_environment(self):
        """A broken environment is exactly when a diagnostic must not add a
        traceback of its own."""
        repo = Path(tempfile.mkdtemp()) / "does-not-exist"
        cfg = config.load(Path(tempfile.mkdtemp()))
        with mock.patch.object(doctor.shutil, "which", return_value=None), \
             mock.patch.object(models, "is_local", return_value=False):
            doctor.check(repo, cfg, (config, harness, models, runrecord))


class ExtensionsCheckTests(unittest.TestCase):
    """What is loaded into the agent, which decides what a run may do.

    Something on the machine this was written on answered for the agent during
    a real run -- `[SECURITY] Blocked command: git (max mode)` -- and nothing
    in the run's record said why. It is `@vtstech/pi-security`, and it is not
    in the extensions directory: it is a package named in pi's `settings.json`,
    which this check could not see, so it reported two files that cannot block
    anything and stayed silent about the one that does.
    """

    def with_extensions(self, *names, packages=None):
        directory = Path(tempfile.mkdtemp())
        (directory / "extensions").mkdir()
        for name in names:
            (directory / "extensions" / name).write_text("//\n")
        if packages is not None:
            (directory / "settings.json").write_text(json.dumps({"packages": packages}))
        adapter = harness.get("pi")
        return mock.patch.object(type(adapter), "config_dir", directory)

    def with_captured_settings(self):
        """The real `settings.json` from the machine this was found on."""
        directory = Path(tempfile.mkdtemp())
        (directory / "extensions").mkdir()
        (directory / "settings.json").write_text(
            (Path(__file__).parent / "testdata" / "pi-settings.json").read_text())
        return mock.patch.object(type(harness.get("pi")), "config_dir", directory)

    def test_installed_extensions_are_named(self):
        with self.with_extensions("model-catalog.js", "moshi-hooks.ts"):
            _, status, detail = doctor.extensions_check(harness, {"agent": {"harness": "pi"}})
        self.assertEqual(doctor.OK, status)
        self.assertIn("model-catalog.js", detail)
        self.assertIn("moshi-hooks.ts", detail)
        self.assertIn("gate what a run may do", detail)

    def test_backups_are_not_counted_as_loaded(self):
        with self.with_extensions("model-catalog.js", "model-catalog.js.bak-20260821"):
            _, _, detail = doctor.extensions_check(harness, {"agent": {"harness": "pi"}})
        self.assertIn("1 loaded", detail)

    def test_an_empty_directory_is_none_installed(self):
        with self.with_extensions():
            _, status, detail = doctor.extensions_check(harness, {"agent": {"harness": "pi"}})
        self.assertEqual(doctor.OK, status)
        self.assertEqual("none installed", detail)

    def test_no_directory_at_all_is_fine(self):
        adapter = harness.get("pi")
        with mock.patch.object(type(adapter), "config_dir", Path(tempfile.mkdtemp())):
            _, status, _ = doctor.extensions_check(harness, {"agent": {"harness": "pi"}})
        self.assertEqual(doctor.OK, status)

    def test_an_agent_whose_config_lmloop_cannot_find_says_so(self):
        _, status, detail = doctor.extensions_check(
            harness, {"agent": {"harness": "opencode"}})
        self.assertEqual(doctor.OK, status)
        self.assertIn("does not know where", detail)

    def test_an_unknown_agent_does_not_raise(self):
        _, status, _ = doctor.extensions_check(harness, {"agent": {"harness": "nonesuch"}})
        self.assertEqual(doctor.OK, status)

    def test_a_package_that_can_block_git_is_named(self):
        """The whole point. Captured rather than written: this is the file the
        check was blind to, exactly as it is on disk -- and with no
        `security.json` beside it, which is the state it was found in and the
        one that blocks `git`."""
        with self.with_captured_settings():
            _, status, detail = doctor.extensions_check(
                harness, {"agent": {"harness": "pi"}})
        self.assertEqual(doctor.WARN, status)
        self.assertIn("npm:@vtstech/pi-security", detail)

    def test_files_and_packages_are_counted_together(self):
        with self.with_extensions("model-catalog.js", packages=["npm:a", "npm:b"]):
            _, _, detail = doctor.extensions_check(harness, {"agent": {"harness": "pi"}})
        self.assertIn("3 loaded", detail)
        for item in ("model-catalog.js", "npm:a", "npm:b"):
            self.assertIn(item, detail)

    def test_an_agent_with_no_package_list_is_unaffected(self):
        """omp keeps a `config.yml`, which this project has no parser for and
        needs none: its extensions are files like anybody else's."""
        self.assertEqual([], harness.get("omp").loaded_packages())

    def test_a_settings_file_that_is_not_what_it_should_be_is_not_fatal(self):
        """doctor exists to report a broken setup, not to become one."""
        for content in ("", "not json", "[]", '{"packages": "everything"}'):
            with self.subTest(content=content):
                directory = Path(tempfile.mkdtemp())
                (directory / "settings.json").write_text(content)
                with mock.patch.object(type(harness.get("pi")), "config_dir", directory):
                    _, status, _ = doctor.extensions_check(
                        harness, {"agent": {"harness": "pi"}})
                self.assertEqual(doctor.OK, status)

    def with_pi_security(self, mode=None):
        """pi with the package that blocks `git` loaded, in a given mode."""
        self.config_dir = Path(tempfile.mkdtemp())
        (self.config_dir / "extensions").mkdir()
        (self.config_dir / "settings.json").write_text(
            json.dumps({"packages": ["npm:@vtstech/pi-security"]}))
        if mode is not None:
            (self.config_dir / "security.json").write_text(json.dumps({"mode": mode}))
        return mock.patch.object(type(harness.get("pi")), "config_dir", self.config_dir)

    def test_a_default_that_blocks_git_is_a_warning_not_a_shrug(self):
        """`max` is what the package uses when nothing is set, and it blocks
        `git`. A loop whose only witness is git will spend iterations working
        around one it is never going to be allowed to run."""
        with self.with_pi_security(mode=None):
            _, status, detail = doctor.extensions_check(
                harness, {"agent": {"harness": "pi"}})
        self.assertEqual(doctor.WARN, status)
        self.assertIn("blocks `git`", detail)

    def test_the_warning_names_the_file_to_change(self):
        """A diagnostic that says "set it somewhere" costs the reader the
        search this check exists to save."""
        with self.with_pi_security(mode="max"):
            _, _, detail = doctor.extensions_check(harness, {"agent": {"harness": "pi"}})
        self.assertIn(str(self.config_dir / "security.json"), detail)

    def test_an_operator_who_has_already_chosen_hears_nothing_further(self):
        for mode in ("basic", "off"):
            with self.subTest(mode=mode), self.with_pi_security(mode=mode):
                _, status, detail = doctor.extensions_check(
                    harness, {"agent": {"harness": "pi"}})
                self.assertEqual(doctor.OK, status)
                self.assertNotIn("blocks `git`", detail)
                # Still named: what is loaded is reported either way.
                self.assertIn("npm:@vtstech/pi-security", detail)

    def test_it_never_judges_an_extension(self):
        """`model-catalog.js` is an extension too, and lmloop would not work
        without it -- reporting is the job, not deciding."""
        with self.with_extensions("model-catalog.js"):
            _, status, _ = doctor.extensions_check(harness, {"agent": {"harness": "pi"}})
        self.assertNotEqual(doctor.FAIL, status)


if __name__ == "__main__":
    unittest.main()
