"""Bounded, shell-free child processes with process-tree cancellation."""

from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

_CAPTURE_CHUNK_BYTES = 64 * 1024
DEFAULT_MAX_OUTPUT_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False
    output_limit_exceeded: bool = False


class ProcessInterrupted(KeyboardInterrupt):
    """Cancellation with the bounded output retained after process cleanup."""

    def __init__(self, result: ProcessResult, *args: object) -> None:
        super().__init__(*args)
        self.result = result


class _OutputLimitExceeded(Exception):
    """Internal signal to stop the process before retaining excess output."""


class _OutputCapture:
    """Read both pipes without waiting for EOF or an inherited handle to close."""

    def __init__(self, process: subprocess.Popen[bytes], limit: int):
        self.limit = limit
        self.stdout = bytearray()
        self.stderr = bytearray()
        self.exceeded = False
        self.streams = [
            (stream, destination)
            for stream, destination in (
                (process.stdout, self.stdout),
                (process.stderr, self.stderr),
            )
            if stream is not None
        ]
        self._peek = None
        if _is_windows() and self.streams:
            import ctypes

            self._peek = ctypes.WinDLL("kernel32", use_last_error=True).PeekNamedPipe
            self._peek.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.c_void_p,
            ]
            self._peek.restype = ctypes.c_int
        elif not _is_windows():
            for stream, _ in self.streams:
                os.set_blocking(stream.fileno(), False)

    def _read(self, stream: BinaryIO) -> bytes | None:
        size = _CAPTURE_CHUNK_BYTES
        if self._peek is not None:
            import ctypes
            import msvcrt

            available = ctypes.c_uint32()
            if not self._peek(
                msvcrt.get_osfhandle(stream.fileno()),
                None,
                0,
                None,
                ctypes.byref(available),
                None,
            ):
                error = ctypes.get_last_error()
                if error == 109:  # ERROR_BROKEN_PIPE: every writer has closed.
                    return b""
                raise ctypes.WinError(error)
            if not available.value:
                return None
            size = min(size, available.value)
        try:
            return os.read(stream.fileno(), size)
        except BlockingIOError:
            return None

    def drain(self) -> bool:
        """Read one bounded chunk per stream, allowing timely timeout checks."""
        active = False
        for stream, destination in self.streams[:]:
            chunk = self._read(stream)
            if chunk is None:
                continue
            if not chunk:
                self.streams.remove((stream, destination))
                continue
            active = True
            remaining = self.limit - len(self.stdout) - len(self.stderr)
            destination.extend(chunk[:remaining])
            if len(chunk) > remaining:
                self.exceeded = True
                raise _OutputLimitExceeded
        return active


def _is_windows() -> bool:
    return os.name == "nt"


def _stop_tree(process: subprocess.Popen[bytes]) -> None:
    """Stop the owned process group before reaping its leader when possible.

    On Windows the tracked process is our Job supervisor. Terminating its
    process handle closes the Job, killing members without targeting PIDs.
    """
    if not _is_windows():
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        # Kill before wait()/poll() can reap the group leader. With waitid's
        # WNOWAIT observation the unreaped leader pins the process-group ID.
        # Descendants may ignore TERM even when the leader has already exited.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=2)


def _wait_for_exit(
    process: subprocess.Popen[bytes], timeout: float, capture: _OutputCapture | None = None
) -> None:
    """Observe POSIX exit without reaping, retaining identity for group cleanup."""
    preserve_identity = not _is_windows() and all(
        hasattr(os, name) for name in ("waitid", "P_PID", "WEXITED", "WNOHANG", "WNOWAIT")
    )
    deadline = time.monotonic() + timeout
    while True:
        active = capture.drain() if capture is not None else False
        # Platforms without WNOWAIT retain best-effort group cleanup after poll.
        exited = (
            os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
            if preserve_identity
            else process.poll() is not None
        )
        if exited:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        if not active:
            time.sleep(min(0.02, remaining))


def _windows_result(path: Path, stdout: str, stderr: str, duration: float) -> ProcessResult:
    """Read the supervisor's private status channel; fail closed if missing."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    error = data.get("os_error")
    if isinstance(error, dict):
        raise OSError(
            error.get("errno"), error.get("message", "Command launch failed"), error.get("filename")
        )
    if type(data.get("returncode")) is int:
        return ProcessResult(data["returncode"], stdout, stderr, duration)
    reason = data.get("setup_error", "Windows Job supervisor did not report a command result")
    return ProcessResult(125, stdout, stderr + f"\n{reason}\n", duration)


def run_process(
    argv: list[str],
    cwd: Path,
    timeout: float,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    *,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> ProcessResult:
    """Run an argument vector and clean up its owned group on every exit.

    Both pipes are drained without blocking, sharing one raw-byte output cap.
    Crossing the cap stops the process and marks the retained prefix incomplete.
    Output is decoded as UTF-8 with replacement; captured newlines are preserved.
    Capture never waits for inherited handles to reach EOF. ``env``, when set,
    is the complete child environment, as with ``subprocess.Popen``. On POSIX,
    remaining group members are terminated even after successful completion.
    On Windows a helper joins a kill-on-close Job before launching the command.
    Job setup failures prevent command launch; no external shell is needed.
    """
    if not argv or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("A command and a finite positive timeout are required")
    if type(max_output_bytes) is not int or max_output_bytes <= 0:
        raise ValueError("max_output_bytes must be a positive integer")
    started = time.monotonic()
    with (
        tempfile.TemporaryDirectory(prefix="bb-process-") as private_directory,
        tempfile.TemporaryFile() as stdin,
    ):
        if input_text is not None:
            stdin.write(input_text.encode("utf-8"))
        stdin.seek(0)
        result_path = Path(private_directory) / "result.json"
        if _is_windows():
            command = [
                sys.executable,
                str(Path(__file__).with_name("_windows_job.py").resolve()),
                str(result_path),
                *argv,
            ]
            options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        else:
            command = argv
            options = {"start_new_session": True}
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            shell=False,
            **options,
        )
        timed_out = False
        interrupted = None
        capture = None
        try:
            capture = _OutputCapture(process, max_output_bytes)
            _wait_for_exit(process, timeout, capture)
        except KeyboardInterrupt as exc:
            interrupted = exc
        except subprocess.TimeoutExpired:
            timed_out = True
        except _OutputLimitExceeded:
            pass
        finally:
            try:
                _stop_tree(process)
                if capture is not None and not capture.exceeded:
                    try:
                        while capture.drain():
                            pass
                    except _OutputLimitExceeded:
                        pass
            finally:
                # Close the original pipes too, including EOF and setup failures.
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
        captured_out = capture.stdout.decode("utf-8", errors="replace") if capture else ""
        captured_err = capture.stderr.decode("utf-8", errors="replace") if capture else ""
        duration = time.monotonic() - started
        exceeded = capture.exceeded if capture else False
        if _is_windows() and not timed_out and not exceeded and interrupted is None:
            return _windows_result(result_path, captured_out, captured_err, duration)
        result = ProcessResult(
            returncode=process.returncode,
            stdout=captured_out,
            stderr=captured_err,
            duration=duration,
            timed_out=timed_out,
            output_limit_exceeded=exceeded,
        )
        if interrupted is not None:
            raise ProcessInterrupted(result, *interrupted.args) from interrupted
        return result
