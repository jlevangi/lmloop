import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import loop
import pi_runner


class CodingAgentLifecycleTests(unittest.TestCase):
    def test_second_signal_still_cleans_the_agent_process_group(self):
        class Agent:
            def argv(self, **kwargs):
                return ["fake-agent"]

        class Process:
            stdin = mock.Mock()
            stdout = object()
            stderr = object()

            def poll(self):
                return None

            def wait(self, **kwargs):
                return 0

        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(pi_runner.harness, "get", return_value=Agent()), \
                 mock.patch.object(pi_runner.subprocess, "Popen", return_value=Process()), \
                 mock.patch.object(pi_runner, "_read_stdout"), \
                 mock.patch.object(pi_runner, "_read_stderr"), \
                 mock.patch.object(pi_runner.time, "sleep"), \
                 mock.patch.object(pi_runner, "_terminate") as terminate:
                with self.assertRaises(KeyboardInterrupt):
                    pi_runner.run(
                        model="model", tools="", thinking="", prompt="prompt",
                        cwd=Path(directory), session_dir=Path(directory),
                        session_id="session", raw_path=Path(directory) / "raw.jsonl",
                        timeout_seconds=600, stall_seconds=600,
                        on_progress=lambda _snapshot: (_ for _ in ()).throw(KeyboardInterrupt),
                    )

        terminate.assert_called_once()


class GateLifecycleTests(unittest.TestCase):
    def test_gate_timeout_terminates_the_descendant_process_group(self):
        run = loop.Run.__new__(loop.Run)
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            gate_log = worktree / "gate.log"
            run.worktree = worktree
            run.config = {"gate": {"command": "run-gate"}}
            run.env = mock.Mock(return_value={})
            run.rundir = mock.Mock()
            run.rundir.gate_log.return_value = gate_log
            process = mock.Mock()
            process.communicate.side_effect = [
                subprocess.TimeoutExpired("run-gate", 600), ("", "")
            ]
            process.wait.side_effect = [subprocess.TimeoutExpired("run-gate", 0), 0]
            process.returncode = -signal.SIGKILL
            with mock.patch.object(loop.subprocess, "Popen", return_value=process) as popen, \
                 mock.patch.object(loop.os, "getpgid", return_value=1234), \
                 mock.patch.object(loop.os, "killpg") as killpg, \
                 mock.patch.object(loop, "GATE_TERM_GRACE_SECONDS", 0):
                run.run_gate(1)

        self.assertEqual("fail (timeout)", run.gate_result)
        popen.assert_called_once()
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        killpg.assert_any_call(1234, signal.SIGTERM)
        killpg.assert_any_call(1234, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
