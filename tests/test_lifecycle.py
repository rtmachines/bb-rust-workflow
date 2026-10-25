"""Live telemetry and scoped signal behavior, without provider sessions."""

import json
import os
import signal
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bb_workflow import lifecycle


class ProgressTests(unittest.TestCase):
    def test_heartbeat_advances_without_touching_authoritative_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            state = directory / "state.json"
            state.write_text('{"status":"running","agent_calls":0}')
            operation = {"role": "implementation"}
            with lifecycle.ProgressTracker(directory, heartbeat_interval=0.02) as tracker:
                tracker.update("implementation", operation)
                operation["role"] = "caller mutation"
                first = json.loads((directory / "progress.json").read_text())
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    second = json.loads((directory / "progress.json").read_text())
                    if second["updated_at"] != first["updated_at"]:
                        break
                    time.sleep(0.01)
                self.assertGreater(second["elapsed_seconds"], first["elapsed_seconds"])
                self.assertEqual(second["active_operation"], {"role": "implementation"})
                self.assertEqual(second["version"], 1)
                self.assertEqual(second["run_dir"], str(directory))
                self.assertEqual(second["pid"], os.getpid())
                self.assertEqual(second["heartbeat_interval_seconds"], 0.02)
            self.assertEqual(state.read_text(), '{"status":"running","agent_calls":0}')
            final = (directory / "progress.json").read_bytes()
            time.sleep(0.04)
            self.assertEqual((directory / "progress.json").read_bytes(), final)

    def test_background_sidecar_failure_is_reported_on_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with self.assertRaisesRegex(OSError, "Progress persistence failed"):
                with lifecycle.ProgressTracker(directory, heartbeat_interval=0.01):
                    with patch(
                        "bb_workflow.lifecycle.write_json", side_effect=OSError("disk full")
                    ):
                        time.sleep(0.05)

    def test_stuck_heartbeat_has_bounded_shutdown_and_reports_failure(self):
        from bb_workflow.workspace import write_json

        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def blocked_write(path, value):
            if threading.current_thread().name == "bb-progress":
                entered.set()
                release.wait(5)
                try:
                    write_json(path, value)
                finally:
                    finished.set()
            else:
                write_json(path, value)

        with tempfile.TemporaryDirectory() as temporary:
            try:
                started = time.monotonic()
                with patch("bb_workflow.lifecycle.write_json", side_effect=blocked_write):
                    with self.assertRaisesRegex(OSError, "heartbeat did not stop"):
                        with lifecycle.ProgressTracker(Path(temporary), heartbeat_interval=0.01):
                            self.assertTrue(entered.wait(2))
                self.assertLess(time.monotonic() - started, 2)
            finally:
                release.set()
                self.assertTrue(finished.wait(2))

    @unittest.skipUnless(os.name == "posix", "POSIX signals")
    def test_sigterm_interrupts_once_and_restores_handler_after_cleanup(self):
        previous = signal.getsignal(signal.SIGTERM)
        with lifecycle.termination_as_interrupt():
            with self.assertRaises(KeyboardInterrupt):
                signal.raise_signal(signal.SIGTERM)
            signal.raise_signal(signal.SIGTERM)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_signal_scope_is_safe_in_worker_thread(self):
        failures = []

        def worker():
            try:
                with lifecycle.termination_as_interrupt():
                    pass
            except BaseException as exc:
                failures.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(2)
        self.assertFalse(failures)
