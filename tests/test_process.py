"""Integration tests use Python children, never a model CLI."""

import ctypes
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bb_workflow.process import ProcessResult, run_process


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cwd = Path(self.temporary.name).resolve()

    def test_utf8_input_output_cwd_environment_and_literal_arguments(self):
        code = (
            "import os,sys; sys.stdout.reconfigure(encoding='utf-8'); "
            "sys.stderr.reconfigure(encoding='utf-8'); print(sys.stdin.buffer.read().decode('utf-8')); print(sys.argv[1]); "
            "print(os.getcwd()); print(os.environ['BB_TEST']); "
            "print('błąd', file=sys.stderr)"
        )
        result = run_process(
            [sys.executable, "-c", code, "$(should-not-run); literal"],
            self.cwd,
            5,
            input_text="zażółć",
            env={**os.environ, "BB_TEST": "works"},
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn(f"zażółć{os.linesep}$(should-not-run); literal", result.stdout)
        self.assertIn(str(self.cwd), result.stdout)
        self.assertIn("works", result.stdout)
        self.assertEqual(result.stderr, f"błąd{os.linesep}")
        self.assertFalse(result.timed_out)
        self.assertGreaterEqual(result.duration, 0)

    def test_capture_preserves_raw_mixed_newlines_in_both_streams(self):
        result = run_process(
            [
                sys.executable,
                "-c",
                "import os; os.write(1, b'first\\r\\nsecond\\nprogress\\r'); "
                "os.write(2, b'error\\r\\n')",
            ],
            self.cwd,
            5,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "first\r\nsecond\nprogress\r")
        self.assertEqual(result.stderr, "error\r\n")

    def test_large_simultaneous_streams_do_not_deadlock(self):
        result = run_process(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('a'*200000); sys.stderr.write('b'*200000)",
            ],
            self.cwd,
            5,
        )
        self.assertEqual(len(result.stdout), 200000)
        self.assertEqual(len(result.stderr), 200000)

    def test_output_exactly_at_combined_byte_boundary_is_complete(self):
        result = run_process(
            [sys.executable, "-c", "import os; os.write(1,b'a'*600); os.write(2,b'b'*424)"],
            self.cwd,
            5,
            max_output_bytes=1024,
        )
        self.assertEqual((result.stdout, result.stderr), ("a" * 600, "b" * 424))
        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.output_limit_exceeded)

    def test_limit_is_shared_across_streams_and_one_extra_byte_is_explicit(self):
        result = run_process(
            [sys.executable, "-c", "import os; os.write(1,b'a'*600); os.write(2,b'b'*425)"],
            self.cwd,
            5,
            max_output_bytes=1024,
        )
        self.assertTrue(result.output_limit_exceeded)
        self.assertFalse(result.timed_out)
        self.assertEqual(len(result.stdout) + len(result.stderr), 1024)

    def test_utf8_budget_counts_raw_bytes_and_preserves_protocol_below_cap(self):
        payload = '{"value":"żółć"}'
        encoded = payload.encode("utf-8")
        command = [sys.executable, "-c", f"import os; os.write(1,{encoded!r})"]
        result = run_process(command, self.cwd, 5, max_output_bytes=len(encoded))
        self.assertEqual(json.loads(result.stdout), {"value": "żółć"})
        self.assertFalse(result.output_limit_exceeded)
        result = run_process(command, self.cwd, 5, max_output_bytes=len(encoded) - 1)
        self.assertTrue(result.output_limit_exceeded)
        self.assertEqual(result.stdout, payload[:-1])

    def test_continuous_simultaneous_output_stops_promptly_at_hard_cap(self):
        code = (
            "import os,threading; "
            "threading.Thread(target=lambda: [os.write(2,b'b'*65536) "
            "for _ in iter(int,1)]).start(); "
            "[os.write(1,b'a'*65536) for _ in iter(int,1)]"
        )
        result = run_process([sys.executable, "-c", code], self.cwd, 5, max_output_bytes=32768)
        self.assertTrue(result.output_limit_exceeded)
        self.assertFalse(result.timed_out)
        self.assertEqual(len(result.stdout) + len(result.stderr), 32768)
        self.assertLess(result.duration, 3)

    def test_output_overflow_cleans_up_descendants(self):
        marker = self.cwd / "overflow-child-finished"
        child = (
            "import time,pathlib; "
            f"time.sleep(0.6); pathlib.Path({str(marker)!r}).write_text('leaked')"
        )
        parent = (
            "import subprocess,sys,os,time; "
            f"subprocess.Popen([sys.executable,'-c',{child!r}]); "
            "os.write(1,b'x'*65536); time.sleep(30)"
        )
        result = run_process([sys.executable, "-c", parent], self.cwd, 5, max_output_bytes=1024)
        self.assertTrue(result.output_limit_exceeded)
        time.sleep(0.7)
        self.assertFalse(marker.exists())

    def test_invalid_output_limits_fail_before_spawning(self):
        for limit in (0, -1, True, 1.5, None):
            with (
                self.subTest(limit=limit),
                patch("bb_workflow.process.subprocess.Popen") as spawn,
                self.assertRaisesRegex(ValueError, "max_output_bytes"),
            ):
                run_process([sys.executable], self.cwd, 1, max_output_bytes=limit)
            spawn.assert_not_called()

    def test_result_positional_fields_remain_compatible(self):
        result = ProcessResult(0, "out", "err", 0.5, True)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.output_limit_exceeded)

    def test_nonzero_exit_and_invalid_utf8_are_preserved(self):
        result = run_process(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'\\xff'); sys.exit(7)",
            ],
            self.cwd,
            5,
        )
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, "\ufffd")

    def test_timeout_preserves_partial_output(self):
        timeout = 0.5 if os.name == "nt" else 0.15
        result = run_process(
            [
                sys.executable,
                "-u",
                "-c",
                "import time; print('started'); time.sleep(30)",
            ],
            self.cwd,
            timeout,
        )
        self.assertTrue(result.timed_out)
        self.assertIn("started", result.stdout)
        self.assertNotEqual(result.returncode, 0)
        self.assertLess(result.duration, 4)

    @unittest.skipIf(os.name == "nt", "POSIX process groups")
    def test_timeout_kills_descendant_ignoring_term(self):
        marker = self.cwd / "child-finished"
        child = (
            "import signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            f"time.sleep(0.7); pathlib.Path({str(marker)!r}).write_text('leaked')"
        )
        parent = (
            "import subprocess,sys,time; "
            f"subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(30)"
        )
        result = run_process([sys.executable, "-c", parent], self.cwd, 0.2)
        self.assertTrue(result.timed_out)
        time.sleep(0.8)
        self.assertFalse(marker.exists())

    @unittest.skipIf(os.name == "nt", "POSIX inherited descriptors")
    def test_exited_parent_with_inherited_output_does_not_hold_capture_open(self):
        child = "import time; time.sleep(2)"
        parent = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{child!r}]); print('done')"
        started = time.monotonic()
        result = run_process([sys.executable, "-c", parent], self.cwd, 0.5)
        self.assertEqual(result.returncode, 0)
        self.assertLess(time.monotonic() - started, 1)

    @unittest.skipIf(os.name == "nt", "POSIX escaped inherited descriptors")
    def test_escaped_descendant_inheriting_pipes_does_not_hold_capture_open(self):
        child_pid = self.cwd / "escaped-pid"
        parent = (
            "import subprocess,sys,pathlib; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],"
            "start_new_session=True); "
            f"pathlib.Path({str(child_pid)!r}).write_text(str(child.pid)); print('complete')"
        )
        try:
            result = run_process([sys.executable, "-c", parent], self.cwd, 0.5)
            self.assertEqual(result.stdout, "complete\n")
            self.assertFalse(result.timed_out)
            self.assertLess(result.duration, 1)
        finally:
            if child_pid.exists():
                try:
                    os.killpg(int(child_pid.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_successful_parent_cannot_leave_a_descendant_that_writes_later(self):
        marker = self.cwd / "late-child-write"
        child = (
            "import time,pathlib; "
            f"time.sleep(0.5); pathlib.Path({str(marker)!r}).write_text('leaked')"
        )
        parent = (
            "import subprocess,sys; "
            f"subprocess.Popen([sys.executable,'-c',{child!r}]); print('parent complete')"
        )
        result = run_process([sys.executable, "-c", parent], self.cwd, 2)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.timed_out)
        self.assertIn("parent complete", result.stdout)
        time.sleep(0.6)
        self.assertFalse(marker.exists())

    def test_natural_failure_exit_code_is_preserved_during_descendant_cleanup(self):
        parent = (
            "import subprocess,sys; subprocess.Popen([sys.executable,'-c',"
            "'import time; time.sleep(30)']); sys.exit(7)"
        )
        result = run_process([sys.executable, "-c", parent], self.cwd, 2)
        self.assertEqual(result.returncode, 7)
        self.assertFalse(result.timed_out)

    def test_keyboard_interrupt_terminates_tree_and_reraises(self):
        with patch("bb_workflow.process.subprocess.Popen") as popen:
            popen.return_value.stdout = popen.return_value.stderr = None
            with (
                patch("bb_workflow.process._wait_for_exit", side_effect=KeyboardInterrupt),
                patch("bb_workflow.process._stop_tree") as stop,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    run_process(["fixture"], self.cwd, 1)
                stop.assert_called_once_with(popen.return_value)

    def test_real_keyboard_interrupt_closes_pipes_and_terminates_process(self):
        launched = []
        original_spawn = subprocess.Popen

        def spawn(*args, **kwargs):
            process = original_spawn(*args, **kwargs)
            launched.append(process)
            return process

        with (
            patch("bb_workflow.process.subprocess.Popen", side_effect=spawn),
            patch("bb_workflow.process._wait_for_exit", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            run_process([sys.executable, "-c", "import time; time.sleep(30)"], self.cwd, 1)
        self.assertIsNotNone(launched[0].returncode)
        self.assertTrue(launched[0].stdout.closed)
        self.assertTrue(launched[0].stderr.closed)

    def test_interrupt_retains_captured_output_after_process_and_pipe_cleanup(self):
        from bb_workflow.process import _OutputCapture

        original_drain = _OutputCapture.drain
        launched = []
        original_spawn = subprocess.Popen
        interrupted = False

        def spawn(*args, **kwargs):
            child = original_spawn(*args, **kwargs)
            launched.append(child)
            return child

        def drain(capture):
            nonlocal interrupted
            active = original_drain(capture)
            if (
                not interrupted
                and capture.stdout == b"retained-out"
                and capture.stderr == b"retained-err"
            ):
                interrupted = True
                raise KeyboardInterrupt("cancel fixture")
            return active

        with (
            patch("bb_workflow.process.subprocess.Popen", side_effect=spawn),
            patch.object(_OutputCapture, "drain", drain),
            self.assertRaises(KeyboardInterrupt) as raised,
        ):
            run_process(
                [
                    sys.executable,
                    "-c",
                    "import os,time; os.write(1,b'retained-out'); os.write(2,b'retained-err'); time.sleep(30)",
                ],
                self.cwd,
                5,
            )
        self.assertIsNotNone(launched[0].returncode)
        self.assertTrue(launched[0].stdout.closed)
        self.assertTrue(launched[0].stderr.closed)
        self.assertIsInstance(getattr(raised.exception, "result", None), ProcessResult)
        result = raised.exception.result
        self.assertEqual((result.stdout, result.stderr), ("retained-out", "retained-err"))
        self.assertFalse(result.timed_out)
        self.assertFalse(result.output_limit_exceeded)
        self.assertGreaterEqual(result.duration, 0)
        self.assertEqual(raised.exception.args, ("cancel fixture",))

    def test_interrupt_retains_bounded_prefix_when_output_overflows(self):
        from bb_workflow.process import _OutputCapture, _OutputLimitExceeded

        original_drain = _OutputCapture.drain

        def drain(capture):
            try:
                return original_drain(capture)
            except _OutputLimitExceeded:
                raise KeyboardInterrupt("cancel at cap")

        with (
            patch.object(_OutputCapture, "drain", drain),
            self.assertRaises(KeyboardInterrupt) as raised,
        ):
            run_process(
                [sys.executable, "-c", "import os,time; os.write(1,b'x'*2048); time.sleep(30)"],
                self.cwd,
                5,
                max_output_bytes=1024,
            )
        self.assertEqual(raised.exception.result.stdout, "x" * 1024)
        self.assertEqual(raised.exception.result.stderr, "")
        self.assertTrue(raised.exception.result.output_limit_exceeded)
        self.assertFalse(raised.exception.result.timed_out)

    def test_windows_interrupt_with_empty_output_does_not_require_supervisor_status(self):
        launched = []

        def spawn(argv, **kwargs):
            launched.append(argv)
            return Mock(returncode=0, stdout=None, stderr=None)

        with (
            patch("bb_workflow.process._is_windows", return_value=True),
            patch("bb_workflow.process.subprocess.CREATE_NEW_PROCESS_GROUP", 512, create=True),
            patch("bb_workflow.process.subprocess.Popen", side_effect=spawn),
            patch("bb_workflow.process._wait_for_exit", side_effect=KeyboardInterrupt),
            patch("bb_workflow.process._stop_tree"),
            self.assertRaises(KeyboardInterrupt) as raised,
        ):
            run_process(["fixture-cli"], self.cwd, 1)
        result = raised.exception.result
        self.assertEqual(result.returncode, 0)
        self.assertEqual((result.stdout, result.stderr), ("", ""))
        self.assertFalse(result.timed_out)
        self.assertFalse(result.output_limit_exceeded)
        self.assertFalse(Path(launched[0][2]).parent.exists())

    def test_capture_setup_error_still_stops_process_and_closes_both_pipes(self):
        with (
            patch("bb_workflow.process.subprocess.Popen") as popen,
            patch("bb_workflow.process._OutputCapture", side_effect=RuntimeError("capture failed")),
            patch("bb_workflow.process._stop_tree") as stop,
            self.assertRaisesRegex(RuntimeError, "capture failed"),
        ):
            run_process(["fixture"], self.cwd, 1)
        stop.assert_called_once_with(popen.return_value)
        popen.return_value.stdout.close.assert_called_once()
        popen.return_value.stderr.close.assert_called_once()

    def test_every_exit_path_cleans_up_exactly_once(self):
        import subprocess

        for error in (None, subprocess.TimeoutExpired("fixture", 1), RuntimeError("wait failed")):
            with (
                self.subTest(error=error),
                patch("bb_workflow.process.subprocess.Popen") as popen,
                patch("bb_workflow.process._wait_for_exit", side_effect=error),
                patch("bb_workflow.process._stop_tree") as stop,
            ):
                popen.return_value.returncode = 0
                popen.return_value.stdout = popen.return_value.stderr = None
                if isinstance(error, RuntimeError):
                    with self.assertRaisesRegex(RuntimeError, "wait failed"):
                        run_process(["fixture"], self.cwd, 1)
                else:
                    result = run_process(["fixture"], self.cwd, 1)
                    self.assertEqual(result.timed_out, error is not None)
                stop.assert_called_once_with(popen.return_value)

    def test_missing_executable_surfaces(self):
        with self.assertRaises(FileNotFoundError):
            run_process([str(self.cwd / "does-not-exist")], self.cwd, 1)

    def test_invalid_timeout_fails_before_spawning(self):
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                run_process([sys.executable], self.cwd, timeout)

    def test_windows_cleanup_kills_supervisor_by_handle_without_pid_commands(self):
        from bb_workflow.process import _stop_tree

        with (
            patch("bb_workflow.process.os.name", "nt"),
            patch("bb_workflow.process.subprocess.run") as run,
        ):
            process = unittest.mock.Mock(pid=123)
            process.poll.return_value = None
            _stop_tree(process)
            run.assert_not_called()
            process.kill.assert_called_once()
            process.wait.assert_called_once_with(timeout=2)

    def test_windows_exited_parent_does_not_trigger_pid_based_tree_kill(self):
        from bb_workflow.process import _stop_tree

        with (
            patch("bb_workflow.process.os.name", "nt"),
            patch("bb_workflow.process.subprocess.run") as run,
        ):
            process = unittest.mock.Mock(pid=123)
            process.poll.return_value = 0
            _stop_tree(process)
            run.assert_not_called()
            process.kill.assert_not_called()
            process.wait.assert_called_once_with(timeout=2)

    @unittest.skipUnless(hasattr(os, "WNOWAIT"), "waitid WNOWAIT required")
    def test_posix_cleanup_signals_before_any_reap_can_release_group_identity(self):
        from bb_workflow.process import _stop_tree

        events = []
        with patch(
            "bb_workflow.process.os.killpg", side_effect=lambda *args: events.append("signal")
        ):
            process = unittest.mock.Mock(pid=123)
            process.poll.side_effect = lambda: events.append("poll") or 0
            process.wait.side_effect = lambda **kwargs: events.append("wait") or 0
            _stop_tree(process)
        self.assertEqual(events, ["signal", "signal", "poll", "wait"])

    def test_windows_private_result_preserves_code_and_recreates_missing_executable_error(self):
        from bb_workflow.process import _windows_result

        path = self.cwd / "result.json"
        path.write_text(json.dumps({"returncode": 7}))
        result = _windows_result(path, "out", "err", 0.5)
        self.assertEqual(result.returncode, 7)
        self.assertEqual((result.stdout, result.stderr), ("out", "err"))
        path.write_text(
            json.dumps({"os_error": {"errno": 2, "message": "missing", "filename": "no-cli"}})
        )
        with self.assertRaises(FileNotFoundError) as raised:
            _windows_result(path, "", "", 0.5)
        self.assertEqual(raised.exception.filename, "no-cli")

    def test_windows_missing_or_malformed_status_fails_closed(self):
        from bb_workflow.process import _windows_result

        path = self.cwd / "result.json"
        result = _windows_result(path, "", "", 0.5)
        self.assertEqual(result.returncode, 125)
        for data in ("not JSON", "[]", '{"returncode":true}', '{"setup_error":"Job denied"}'):
            with self.subTest(data=data):
                path.write_text(data)
                result = _windows_result(path, "", "", 0.5)
                self.assertEqual(result.returncode, 125)
                self.assertTrue(result.stderr)

    def test_windows_wrapper_uses_absolute_helper_and_cleans_private_result_directory(self):
        launched = []

        def spawn(argv, **kwargs):
            launched.append((argv, kwargs))
            Path(argv[2]).write_text('{"returncode":0}')
            return Mock(returncode=1, stdout=None, stderr=None)

        with (
            patch("bb_workflow.process._is_windows", return_value=True),
            patch("bb_workflow.process.subprocess.CREATE_NEW_PROCESS_GROUP", 512, create=True),
            patch("bb_workflow.process.subprocess.Popen", side_effect=spawn),
            patch("bb_workflow.process._wait_for_exit"),
            patch("bb_workflow.process._stop_tree"),
        ):
            result = run_process(["fixture-cli", "literal; $(text)"], self.cwd, 1)
        argv, options = launched[0]
        self.assertEqual(argv[0], sys.executable)
        self.assertTrue(Path(argv[1]).is_absolute())
        self.assertEqual(Path(argv[1]).name, "_windows_job.py")
        self.assertEqual(argv[3:], ["fixture-cli", "literal; $(text)"])
        self.assertFalse(options["shell"])
        self.assertEqual(result.returncode, 0)
        self.assertFalse(Path(argv[2]).parent.exists())


class WindowsJobTests(unittest.TestCase):
    def api(self):
        api = Mock()
        api.CreateJobObjectW.return_value = 123
        api.SetInformationJobObject.return_value = 1
        api.AssignProcessToJobObject.return_value = 1
        api.GetCurrentProcess.return_value = -1
        return api

    def test_windows_pipe_reads_only_available_bytes_without_waiting_for_eof(self):
        from bb_workflow.process import _OutputCapture

        capture = object.__new__(_OutputCapture)
        stream = Mock()
        stream.fileno.return_value = 7
        available = 0

        def peek(handle, buffer, size, read, pointer, remaining):
            self.assertEqual(handle, 77)
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32)).contents.value = available
            return 1

        capture._peek = Mock(side_effect=peek)
        with (
            patch.dict(sys.modules, {"msvcrt": Mock(get_osfhandle=Mock(return_value=77))}),
            patch("bb_workflow.process.os.read", return_value=b"output") as read,
        ):
            self.assertIsNone(capture._read(stream))
            read.assert_not_called()
            available = 6
            self.assertEqual(capture._read(stream), b"output")
            read.assert_called_once_with(7, 6)

    def test_windows_broken_pipe_is_eof_and_other_pipe_errors_surface(self):
        from bb_workflow.process import _OutputCapture

        capture = object.__new__(_OutputCapture)
        capture._peek = Mock(return_value=0)
        stream = Mock()
        with (
            patch.dict(sys.modules, {"msvcrt": Mock()}),
            patch("ctypes.get_last_error", return_value=109, create=True) as last_error,
            patch("ctypes.WinError", return_value=OSError("pipe failed"), create=True),
            patch("bb_workflow.process.os.read") as read,
        ):
            self.assertEqual(capture._read(stream), b"")
            last_error.return_value = 5
            with self.assertRaisesRegex(OSError, "pipe failed"):
                capture._read(stream)
            read.assert_not_called()

    def test_job_setup_uses_noninherited_handle_kill_on_close_and_self_assignment(self):
        from bb_workflow._windows_job import _create_job, _ExtendedLimits

        api = self.api()

        def inspect_limits(job, information_class, pointer, size):
            self.assertEqual(job, 123)
            self.assertEqual(information_class, 9)
            self.assertEqual(size, ctypes.sizeof(_ExtendedLimits))
            limits = ctypes.cast(pointer, ctypes.POINTER(_ExtendedLimits)).contents
            self.assertEqual(limits.BasicLimitInformation.LimitFlags, 0x2000)
            return 1

        api.SetInformationJobObject.side_effect = inspect_limits
        self.assertEqual(_create_job(api), 123)
        api.CreateJobObjectW.assert_called_once_with(None, None)
        api.AssignProcessToJobObject.assert_called_once_with(123, -1)
        api.CloseHandle.assert_not_called()

    def test_job_setup_failures_close_created_handle_and_never_assign_early(self):
        from bb_workflow._windows_job import _create_job

        for failing in ("CreateJobObjectW", "SetInformationJobObject", "AssignProcessToJobObject"):
            with (
                self.subTest(failing=failing),
                patch("ctypes.get_last_error", return_value=5, create=True),
            ):
                api = self.api()
                getattr(api, failing).return_value = 0
                with self.assertRaises(OSError):
                    _create_job(api)
                if failing == "CreateJobObjectW":
                    api.CloseHandle.assert_not_called()
                    api.SetInformationJobObject.assert_not_called()
                else:
                    api.CloseHandle.assert_called_once_with(123)
                if failing != "AssignProcessToJobObject":
                    api.AssignProcessToJobObject.assert_not_called()

    def test_supervisor_assigns_job_before_provider_and_preserves_return_code(self):
        from bb_workflow._windows_job import main

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "result.json"
            events = []

            def create_job():
                events.append("job")
                return 123

            def spawn(argv, **kwargs):
                events.append("provider")
                self.assertEqual(argv, ["fixture", "argument with spaces"])
                self.assertFalse(kwargs["shell"])
                self.assertIs(kwargs["stdin"], sys.stdin.buffer)
                self.assertIs(kwargs["stdout"], sys.stdout.buffer)
                self.assertIs(kwargs["stderr"], sys.stderr.buffer)
                return Mock(wait=Mock(return_value=7))

            with (
                patch("bb_workflow._windows_job._create_job", side_effect=create_job),
                patch("bb_workflow._windows_job.subprocess.Popen", side_effect=spawn),
            ):
                code = main([str(path), "fixture", "argument with spaces"])
            self.assertEqual(events, ["job", "provider"])
            self.assertEqual(code, 7)
            self.assertEqual(json.loads(path.read_text()), {"returncode": 7})
            self.assertFalse(path.with_suffix(".tmp").exists())

    def test_supervisor_setup_failure_prevents_provider_launch(self):
        from bb_workflow._windows_job import main

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "result.json"
            with (
                patch("bb_workflow._windows_job._create_job", side_effect=OSError(5, "denied")),
                patch("bb_workflow._windows_job.subprocess.Popen") as spawn,
            ):
                self.assertEqual(main([str(path), "fixture"]), 125)
            spawn.assert_not_called()
            self.assertIn("setup_error", json.loads(path.read_text()))

    def test_supervisor_serializes_launch_os_error(self):
        from bb_workflow._windows_job import main

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "result.json"
            with (
                patch("bb_workflow._windows_job._create_job", return_value=123),
                patch(
                    "bb_workflow._windows_job.subprocess.Popen",
                    side_effect=FileNotFoundError(2, "missing", "fixture"),
                ),
            ):
                self.assertEqual(main([str(path), "fixture"]), 126)
            error = json.loads(path.read_text())["os_error"]
            self.assertEqual(error["errno"], 2)
            self.assertEqual(error["filename"], "fixture")


if __name__ == "__main__":
    unittest.main()
