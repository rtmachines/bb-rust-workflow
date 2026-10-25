"""Windows-only child supervisor; absolute filename, without package imports.

The supervisor joins its own kill-on-close Job before starting the command.
The unnamed Job handle is noninheritable and remains open until supervisor
exit. Windows then terminates remaining Job members, including descendants
of a successfully completed command. Setup failures never launch a command.

See https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
and the documented JOBOBJECT_EXTENDED_LIMIT_INFORMATION structure.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path

_HANDLE = ctypes.c_void_p
_DWORD = ctypes.c_uint32
_BOOL = ctypes.c_int32
_SIZE_T = ctypes.c_size_t
_KILL_ON_JOB_CLOSE = 0x00002000
_EXTENDED_LIMIT_INFORMATION = 9


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", _DWORD),
        ("MinimumWorkingSetSize", _SIZE_T),
        ("MaximumWorkingSetSize", _SIZE_T),
        ("ActiveProcessLimit", _DWORD),
        ("Affinity", _SIZE_T),
        ("PriorityClass", _DWORD),
        ("SchedulingClass", _DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", _SIZE_T),
        ("JobMemoryLimit", _SIZE_T),
        ("PeakProcessMemoryUsed", _SIZE_T),
        ("PeakJobMemoryUsed", _SIZE_T),
    ]


def _create_job(kernel32=None) -> int:
    if kernel32 is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([_HANDLE, ctypes.c_wchar_p], _HANDLE),
        "SetInformationJobObject": ([_HANDLE, ctypes.c_int, _HANDLE, _DWORD], _BOOL),
        "AssignProcessToJobObject": ([_HANDLE, _HANDLE], _BOOL),
        "GetCurrentProcess": ([], _HANDLE),
        "CloseHandle": ([_HANDLE], _BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel32, name)
        function.argtypes = arguments
        function.restype = result

    def error(operation):
        return OSError(ctypes.get_last_error(), f"Windows Job {operation} failed")

    # NULL security attributes make this handle noninheritable. No name means
    # another process cannot attach by discovering a predictable object name.
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise error("creation")
    limits = _ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        job, _EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)
    ):
        failure = error("configuration")
        kernel32.CloseHandle(job)
        raise failure
    if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
        failure = error("assignment (nested jobs must be permitted)")
        kernel32.CloseHandle(job)
        raise failure
    # Do not CloseHandle after successful assignment: the supervisor itself
    # belongs to this job. Kernel teardown on process exit closes it instead.
    return job


def _write_result(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=True, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def main(arguments: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if arguments is None else arguments
    if len(arguments) < 2:
        return 125
    result_path, argv = Path(arguments[0]), arguments[1:]
    try:
        job = _create_job()
    except OSError as exc:
        _write_result(result_path, {"setup_error": str(exc)})
        return 125
    # Retain the integer handle and never hand it to children. No provider can
    # start until this process has successfully joined its Job.
    assert job
    try:
        process = subprocess.Popen(
            argv,
            shell=False,
            stdin=sys.stdin.buffer,
            stdout=sys.stdout.buffer,
            stderr=sys.stderr.buffer,
        )
    except OSError as exc:
        _write_result(
            result_path,
            {
                "os_error": {
                    "errno": exc.errno,
                    "message": str(exc),
                    "filename": exc.filename or argv[0],
                }
            },
        )
        return 126
    returncode = process.wait()
    # The explicit result preserves the command's code even if closing this
    # supervisor's Job handle changes the supervisor's own termination code.
    _write_result(result_path, {"returncode": returncode})
    return returncode


if __name__ == "__main__":
    os._exit(main())
