"""Advisory run telemetry and scoped command-line termination handling."""

from __future__ import annotations

import json
import math
import os
import signal
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from .workspace import write_json


class ProgressTracker:
    """Own copy-on-update metadata; the heartbeat never reads or writes workflow state."""

    def __init__(self, run_dir: Path, *, heartbeat_interval: float = 5.0):
        if not math.isfinite(heartbeat_interval) or heartbeat_interval <= 0:
            raise ValueError("A finite positive heartbeat interval is required")
        self.run_dir = run_dir.resolve()
        self.interval = heartbeat_interval
        self.started = time.monotonic()
        self._metadata = {"phase": "preparing", "active_operation": None}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._error: Exception | None = None
        self._thread = threading.Thread(target=self._heartbeat, daemon=True, name="bb-progress")

    def __enter__(self):
        self._write()
        self._thread.start()
        return self

    def _write(self):
        try:
            with self._lock:
                if self._error is not None:
                    raise self._error
                write_json(
                    self.run_dir / "progress.json",
                    {
                        "version": 1,
                        "run_dir": str(self.run_dir),
                        "pid": os.getpid(),
                        **self._metadata,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "elapsed_seconds": round(time.monotonic() - self.started, 3),
                        "heartbeat_interval_seconds": self.interval,
                    },
                )
        except Exception as exc:
            self._error = exc
            self._stop.set()
            raise OSError(f"Progress persistence failed: {exc}") from exc

    def update(self, phase: str, active_operation: dict | None = None):
        with self._lock:
            self._metadata = {"phase": phase, "active_operation": deepcopy(active_operation)}
        self._write()

    def _heartbeat(self):
        while not self._stop.wait(self.interval):
            try:
                self._write()
            except OSError:
                return

    def __exit__(self, exc_type, exc, traceback):
        self._stop.set()
        self._thread.join(timeout=1.0)
        if exc_type is None:
            if self._thread.is_alive():
                raise OSError("Progress persistence failed: heartbeat did not stop")
            if self._error is not None:
                raise OSError(f"Progress persistence failed: {self._error}") from self._error


def read_progress(run_dir: Path) -> dict | None:
    """Read untrusted advisory metadata; malformed or foreign sidecars are ignored."""
    try:
        value = json.loads((run_dir / "progress.json").read_text(encoding="utf-8"))
        # Reject non-finite values in operations and future/unknown metadata too.
        json.dumps(value, allow_nan=False)
        if not isinstance(value, dict):
            return None
        if type(value.get("version")) is not int or value["version"] != 1:
            return None
        if value.get("run_dir") != str(run_dir.resolve()):
            return None
        if type(value.get("pid")) is not int or value["pid"] <= 0:
            return None
        if not isinstance(value.get("phase"), str):
            return None
        if value.get("active_operation") is not None and not isinstance(
            value["active_operation"], dict
        ):
            return None
        for field in ("elapsed_seconds", "heartbeat_interval_seconds"):
            number = value.get(field)
            if type(number) not in (int, float) or not math.isfinite(number) or number < 0:
                return None
        if value["heartbeat_interval_seconds"] == 0:
            return None
        timestamp = datetime.fromisoformat(value["updated_at"])
        if timestamp.tzinfo is None:
            return None
        return value
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, OverflowError):
        return None


@contextmanager
def termination_as_interrupt():
    """Route one POSIX CLI SIGTERM through existing Ctrl+C cleanup, then restore policy."""
    if os.name != "posix" or threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGTERM)

    def interrupt(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt("Terminated")

    signal.signal(signal.SIGTERM, interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)
