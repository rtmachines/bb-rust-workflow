"""Verified filesystem copies and guarded application of a completed run.

Working copies provide functional isolation, not a security sandbox. The lock
coordinates workflow commands; editors and other programs do not honor it.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import stat
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator

from .process import run_process


class WorkspaceError(RuntimeError):
    """A workspace cannot be safely captured or applied."""


MANDATORY_EXCLUDES = (
    ".git",
    ".bb-workflow",
    ".code-review-graph",
)

DEFAULT_EXCLUDES = (
    "/target",
    "/.venv",
    "/venv",
    "__pycache__",
    "/.pytest_cache",
    "/.ruff_cache",
    "/build",
    "/dist",
    "/node_modules",
    "/*.egg-info",
)


def _mandatory_excluded(relative: str) -> bool:
    return any(part in MANDATORY_EXCLUDES for part in PurePosixPath(relative).parts)


def _excluded(relative: str, exclude: tuple[str, ...]) -> bool:
    """Apply ordered globs; / anchors at the root and ! re-includes paths.

    Unanchored patterns match components anywhere or complete relative paths.
    A matching ancestor also matches its descendants. User patterns override
    built-in cache defaults; mandatory metadata directories always stay excluded.
    """
    if _mandatory_excluded(relative):
        return True
    parts = PurePosixPath(relative).parts
    ancestors = ["/".join(parts[:index]) for index in range(1, len(parts) + 1)]
    excluded = False
    for pattern in (*DEFAULT_EXCLUDES, *exclude):
        include = pattern.startswith("!")
        pattern = pattern[1:] if include else pattern
        anchored = pattern.startswith("/")
        pattern = pattern[1:] if anchored else pattern
        if any(fnmatch.fnmatchcase(path, pattern) for path in ancestors) or (
            not anchored and any(fnmatch.fnmatchcase(part, pattern) for part in parts)
        ):
            excluded = not include
    return excluded


def _validate_relative_path(relative: str, exclude: tuple[str, ...] | None = None) -> None:
    """Reject relative paths that cannot be represented portably by apply."""
    if (
        not relative
        or "\\" in relative
        or ":" in relative
        or "\0" in relative
        or relative.startswith("/")
        or any(part in {"", ".", ".."} for part in relative.split("/"))
        or (exclude is not None and _excluded(relative, exclude))
    ):
        raise WorkspaceError(
            f"unsupported or excluded relative path {relative!r}; rename it or exclude it explicitly"
        )


def _reparse_point(info: os.stat_result) -> bool:
    return bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _directory(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        info = None
    if info is None or _reparse_point(info) or not stat.S_ISDIR(info.st_mode):
        raise WorkspaceError(f"expected a real directory, not a symlink: {path}")


def _regular(path: Path) -> os.stat_result:
    info = path.lstat()
    if _reparse_point(info) or not stat.S_ISREG(info.st_mode):
        raise WorkspaceError(f"unsupported symlink or special file: {path}; exclude it explicitly")
    return info


def _file_hash(path: Path) -> str:
    before = _regular(path)
    # Windows path stat infers execute bits from the suffix; fstat does not.
    shared_mode_mask = ~0o111 if os.name == "nt" else -1
    digest = hashlib.sha256()
    digest.update(f"mode:{stat.S_IMODE(before.st_mode):o}\0".encode("ascii"))
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_mode & shared_mode_mask,
        ) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_mode & shared_mode_mask,
        ):
            raise WorkspaceError(f"file changed while reading: {path}")
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
        after = os.fstat(stream.fileno())

    def signature(info: os.stat_result) -> tuple[int, ...]:
        return (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
            info.st_mode,
        )

    # Windows path stat can report birth time as ctime while fstat reports
    # change time. Compare full metadata within each API, retaining the shared
    # identity/content checks above between the path and its opened handle.
    if signature(before) != signature(path.lstat()) or signature(opened) != signature(after):
        raise WorkspaceError(f"file changed while reading: {path}")
    return digest.hexdigest()


def _scan(root: Path, exclude: tuple[str, ...]) -> tuple[dict[str, str], set[str]]:
    """Collect file hashes and real directories without following symlinks."""
    root = Path(root)
    _directory(root)
    result: dict[str, str] = {}
    directories: set[str] = set()
    unanchored_reinclusion = any(
        pattern.startswith("!") and not pattern.startswith("!/") for pattern in exclude
    )
    reincluded_roots = [
        pattern[2:].split("/")[0] for pattern in exclude if pattern.startswith("!/")
    ]

    def walk(directory: Path) -> None:
        for path in sorted(directory.iterdir()):
            relative = path.relative_to(root).as_posix()
            if _mandatory_excluded(relative):
                continue
            excluded = _excluded(relative, exclude)
            may_reinclude = unanchored_reinclusion or any(
                fnmatch.fnmatchcase(PurePosixPath(relative).parts[0], pattern)
                for pattern in reincluded_roots
            )
            if excluded and not may_reinclude:
                continue
            info = path.lstat()
            if _reparse_point(info):
                if excluded:
                    continue
                raise WorkspaceError(
                    f"unsupported symlink or special file: {path}; exclude it explicitly"
                )
            if stat.S_ISDIR(info.st_mode):
                if not excluded:
                    _validate_relative_path(relative)
                    directories.add(relative)
                walk(path)
            elif excluded:
                continue
            elif stat.S_ISREG(info.st_mode):
                _validate_relative_path(relative)
                result[relative] = _file_hash(path)
            else:
                raise WorkspaceError(
                    f"unsupported symlink or special file: {path}; exclude it explicitly"
                )

    walk(root)
    return result, directories


def manifest(root: Path, exclude: tuple[str, ...] = ()) -> dict[str, str]:
    """Hash regular files and permissions; reject unexcluded links/special files.

    Directory entries are intentionally absent from manifests and snapshot IDs.
    """
    return _scan(root, exclude)[0]


def snapshot_id(files: dict[str, str]) -> str:
    """Return a deterministic identifier for a manifest."""
    return hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def changed_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """List added, modified and removed paths in deterministic order."""
    return sorted(
        path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
    )


def copy_snapshot(source: Path, destination: Path, exclude: tuple[str, ...] = ()) -> dict[str, str]:
    """Copy an unchanged source to a new empty destination, then verify both."""
    source, destination = Path(source), Path(destination)
    before, directories = _scan(source, exclude)
    resolved_source, resolved_destination = source.resolve(), destination.resolve()
    if resolved_destination == resolved_source:
        raise WorkspaceError("snapshot destination must differ from the source")
    if resolved_source in resolved_destination.parents:
        relative = resolved_destination.relative_to(resolved_source).as_posix()
        if not any(
            _excluded(parent.as_posix(), exclude)
            for parent in PurePosixPath(relative).parents
            if str(parent) != "."
        ):
            raise WorkspaceError(
                "snapshot destination inside source must be in an excluded directory"
            )
    if destination.exists() or destination.is_symlink():
        _directory(destination)
        if any(destination.iterdir()):
            raise WorkspaceError(f"snapshot destination must be empty: {destination}")
    else:
        destination.mkdir(parents=True)
    for relative in sorted(directories):
        (destination / relative).mkdir(parents=True, exist_ok=True)
    for relative in before:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        _regular(source / relative)
        shutil.copy2(source / relative, target, follow_symlinks=False)
    if _scan(source, exclude) != (before, directories):
        raise WorkspaceError("source changed while copying; retry with a stable source")
    if _scan(destination, exclude) != (before, directories):
        raise WorkspaceError("snapshot differs from source; capture aborted")
    return before


def snapshot_environment() -> dict[str, str]:
    """Copy the environment without inherited Git repository/config redirection."""
    redirected = {
        "GIT_DIR",
        "GIT_COMMON_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_PREFIX",
        "GIT_SHALLOW_FILE",
        "GIT_REPLACE_REF_BASE",
        "GIT_ATTR_SOURCE",
    }
    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in redirected and not key.upper().startswith("GIT_CONFIG")
    }


def initialize_git_snapshot(root: Path, *, timeout: float = 30) -> None:
    """Give a captured tree its own Git boundary and commit every captured file.

    Source Git metadata is never copied or modified. Local attributes disable
    conversion and filters so baseline blobs retain the captured file bytes.
    """
    root = Path(root).resolve()
    _directory(root)
    metadata = root / ".git"
    if os.path.lexists(metadata):
        raise WorkspaceError(f"snapshot already has Git metadata: {metadata}")
    environment = snapshot_environment()
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "BB Workflow",
            "GIT_AUTHOR_EMAIL": "snapshot@bb-workflow.invalid",
            "GIT_COMMITTER_NAME": "BB Workflow",
            "GIT_COMMITTER_EMAIL": "snapshot@bb-workflow.invalid",
        }
    )
    environment.pop("GIT_AUTHOR_DATE", None)
    environment.pop("GIT_COMMITTER_DATE", None)
    deadline = time.monotonic() + timeout

    def git(*arguments: str) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WorkspaceError(f"Git snapshot initialization timed out: {root}")
        try:
            result = run_process(["git", *arguments], root, remaining, env=environment)
        except FileNotFoundError as exc:
            raise WorkspaceError(
                "Git is required to initialize workflow snapshots; install Git"
            ) from exc
        if result.timed_out:
            raise WorkspaceError(f"Git snapshot initialization timed out: {root}")
        if result.output_limit_exceeded:
            raise WorkspaceError(
                f"Git snapshot initialization exceeded the output byte limit: {root}"
            )
        if result.returncode:
            raise WorkspaceError(
                f"Git snapshot initialization failed ({arguments[0]}): {result.stderr.strip()}"
            )

    git("init", "--quiet", "--template=", "--initial-branch=workflow")
    disabled_hooks = metadata / "disabled-hooks"
    disabled_hooks.mkdir()
    for key, value in (
        ("core.worktree", str(root)),
        ("core.hooksPath", str(disabled_hooks)),
        ("core.autocrlf", "false"),
        ("core.attributesFile", os.devnull),
        ("commit.gpgSign", "false"),
    ):
        git("config", "--local", key, value)
    info = metadata / "info"
    info.mkdir(exist_ok=True)
    (info / "attributes").write_text(
        "* -filter -text -ident -working-tree-encoding\n", encoding="utf-8"
    )
    git("add", "--force", "--all", "--", ".")
    git("commit", "--quiet", "--allow-empty", "--no-gpg-sign", "-m", "Captured workflow baseline")


def write_json(path: Path, data: object) -> None:
    """Atomically replace a JSON file in its existing parent directory."""
    path = Path(path)
    _directory(path.parent)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise WorkspaceError(f"refusing to replace non-regular metadata: {path}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def project_lock(repo: Path) -> Iterator[None]:
    """Hold an exclusive workflow lock; never automatically break stale locks."""
    repo = Path(repo)
    _directory(repo)
    metadata = repo / ".bb-workflow"
    if not metadata.exists() and not metadata.is_symlink():
        metadata.mkdir(exist_ok=True)
    _directory(metadata)
    lock = metadata / "lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise WorkspaceError(
            f"project is locked: {lock}. If no workflow is running, inspect this lock and remove it manually to recover."
        ) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(f"pid={os.getpid()}\n")
        yield
    finally:
        lock.unlink()


def _read_json(path: Path) -> object:
    _regular(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise WorkspaceError(f"invalid JSON metadata: {path}") from exc


def _read_manifest(path: Path, exclude: tuple[str, ...]) -> dict[str, str]:
    value = _read_json(path)
    if not isinstance(value, dict):
        raise WorkspaceError(f"manifest must be an object: {path}")
    for relative, digest in value.items():
        if not isinstance(relative, str):
            raise WorkspaceError(f"invalid manifest path: {relative!r}")
        _validate_relative_path(relative, exclude)
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise WorkspaceError(f"invalid manifest hash for {relative!r}")
    return value


def _preflight(repo: Path, changes: list[str]) -> None:
    _directory(repo)
    for relative in changes:
        target = repo / relative
        for parent in target.parents:
            if parent == repo:
                break
            try:
                info = parent.lstat()
            except FileNotFoundError:
                continue
            if _reparse_point(info) or not stat.S_ISDIR(info.st_mode):
                raise WorkspaceError(
                    f"file/directory transition is unsupported; cannot apply {relative}"
                )
        try:
            info = target.lstat()
        except FileNotFoundError:
            continue
        if _reparse_point(info) or not stat.S_ISREG(info.st_mode):
            raise WorkspaceError(
                f"file/directory transition is unsupported; cannot apply {relative}"
            )


def _replace_file(
    source: Path,
    target: Path,
    completed: Callable[[tuple[int, int]], None] | None = None,
    *,
    expected_digest: str | None = None,
) -> None:
    """Replace a regular file without writing through pre-existing hardlinks."""
    _regular(source)
    descriptor, temporary = tempfile.mkstemp(prefix=".bb-apply-", dir=target.parent)
    os.close(descriptor)
    try:
        shutil.copy2(source, temporary, follow_symlinks=False)
        info = _regular(Path(temporary))
        if expected_digest is not None and _file_hash(Path(temporary)) != expected_digest:
            raise WorkspaceError(f"source changed while preparing replacement: {source}")
        os.replace(temporary, target)
        if completed is not None:
            completed((info.st_dev, info.st_ino))
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


@dataclass(frozen=True)
class _AppliedChange:
    digest: str | None
    identity: tuple[int, int] | None = None


def _matches_applied(target: Path, expected: _AppliedChange) -> bool:
    if expected.digest is None:
        return not os.path.lexists(target)
    if not os.path.lexists(target) or _file_hash(target) != expected.digest:
        return False
    info = target.lstat()
    return expected.identity is None or (info.st_dev, info.st_ino) == expected.identity


def _restore(
    repo: Path,
    backup: Path,
    changed: list[str],
    baseline: dict[str, str],
    created: list[Path],
    completed: dict[str, _AppliedChange],
) -> None:
    errors = []
    for relative in reversed(changed):
        target = repo / relative
        try:
            _preflight(repo, [relative])
            expected = completed.get(relative, _AppliedChange(baseline.get(relative)))
            if not _matches_applied(target, expected):
                errors.append(f"preserved concurrent change: {relative}")
                continue
            if relative not in completed:
                continue
            # This guard is best effort: portable filesystems provide no atomic
            # compare-and-replace against an editor changing the path here.
            if relative in baseline:
                _replace_file(backup / relative, target, expected_digest=baseline[relative])
            else:
                target.unlink()
        except BaseException as exc:
            errors.append(f"{relative}: {type(exc).__name__}: {exc}")
    for directory in reversed(created):
        try:
            if not os.path.lexists(directory):
                continue
            for parent in (directory, *directory.parents):
                if parent == repo:
                    break
                _directory(parent)
            directory.rmdir()
        except FileNotFoundError:
            pass
        except BaseException as exc:
            errors.append(f"{directory}: {type(exc).__name__}: {exc}")
    if errors:
        raise WorkspaceError("; ".join(errors))


def apply_run(run_dir: Path) -> list[str]:
    """Apply a verified successful run only while its original baseline matches.

    File/directory transitions are rejected before mutation. Backups and an
    apply.json report remain available if copying or report writing fails.
    Empty directories are preserved in copies but are not synchronized by apply.
    """
    run_dir = Path(run_dir).absolute()
    _directory(run_dir)
    if run_dir.resolve() != run_dir:
        raise WorkspaceError("run directory must use its canonical path without symlinks")
    state = _read_json(run_dir / "state.json")
    if not isinstance(state, dict) or state.get("status") != "succeeded":
        raise WorkspaceError("only a succeeded run can be applied")
    repo_value, excludes = state.get("repo"), state.get("exclude")
    if not isinstance(repo_value, str) or not Path(repo_value).is_absolute():
        raise WorkspaceError("run repo must be an absolute path")
    repo = Path(repo_value)
    if repo.resolve() != repo or repo / ".bb-workflow" not in run_dir.parents:
        raise WorkspaceError(
            "run directory must be inside its recorded repository's .bb-workflow directory"
        )
    if not isinstance(excludes, list) or any(
        not isinstance(item, str) or not item for item in excludes
    ):
        raise WorkspaceError("run exclude must be an array of non-empty strings")
    exclude = tuple(excludes)
    with project_lock(repo):
        # Lazy import avoids requirements -> verification -> workspace import cycle.
        from .acceptance import validate_acceptance

        validate_acceptance(run_dir, state)
        baseline = _read_manifest(run_dir / "baseline_manifest.json", exclude)
        candidate = _read_manifest(run_dir / "candidate_manifest.json", exclude)
        if snapshot_id(baseline) != state.get("baseline_id") or snapshot_id(candidate) != state.get(
            "candidate_id"
        ):
            raise WorkspaceError("run manifest metadata changed; refusing to apply")
        workspace = run_dir / "workspace"
        if manifest(workspace, exclude) != candidate:
            raise WorkspaceError("candidate workspace changed after validation; refusing to apply")
        if manifest(repo, exclude) != baseline:
            raise WorkspaceError(
                "original repository changed since capture; refusing to overwrite existing edits"
            )
        changes = changed_paths(baseline, candidate)
        _preflight(repo, changes)
        backup = run_dir / "apply-backup"
        if backup.exists() or backup.is_symlink():
            raise WorkspaceError(
                f"apply backup already exists; inspect it before recovery: {backup}"
            )
        backup.mkdir()
        for relative in changes:
            if relative in baseline:
                target = backup / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(repo / relative, target, follow_symlinks=False)
                if _file_hash(target) != baseline[relative]:
                    raise WorkspaceError(f"source changed while backing up: {relative}")
        # Check again after the potentially long backup operation, before mutation.
        if manifest(repo, exclude) != baseline or manifest(workspace, exclude) != candidate:
            raise WorkspaceError("repository or candidate changed while preparing apply")
        completed: dict[str, _AppliedChange] = {}
        created: list[Path] = []
        try:
            for relative in changes:
                target = repo / relative
                _preflight(repo, [relative])
                if not _matches_applied(target, _AppliedChange(baseline.get(relative))):
                    raise WorkspaceError(f"repository changed before applying: {relative}")
                if relative in candidate:
                    missing = []
                    parent = target.parent
                    while not parent.exists():
                        missing.append(parent)
                        parent = parent.parent
                    for directory in reversed(missing):
                        directory.mkdir()
                        created.append(directory)

                    def record_replacement(identity: tuple[int, int]) -> None:
                        completed[relative] = _AppliedChange(candidate[relative], identity)

                    _replace_file(
                        workspace / relative,
                        target,
                        record_replacement,
                        expected_digest=candidate[relative],
                    )
                else:
                    target.unlink()
                    completed[relative] = _AppliedChange(None)
            if manifest(repo, exclude) != candidate:
                raise WorkspaceError("repository differs from candidate after apply")
            write_json(run_dir / "apply.json", {"status": "applied", "paths": changes})
            write_json(
                run_dir / "state.json", {**state, "status": "applied", "applied_paths": changes}
            )
        except BaseException as exc:
            rollback_error = None
            try:
                _restore(repo, backup, changes, baseline, created, completed)
            except BaseException as rollback:
                rollback_error = str(rollback)
            report = {
                "status": "failed",
                "paths": changes,
                "error": str(exc),
                "rollback_error": rollback_error,
                "backup": str(backup),
            }
            try:
                write_json(run_dir / "apply.json", report)
            except (OSError, WorkspaceError):
                pass
            if rollback_error is not None:
                raise WorkspaceError(
                    f"apply failed and rollback was incomplete; recover from {backup}: {rollback_error}"
                ) from exc
            raise
        return changes
