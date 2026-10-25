"""Filesystem integration tests: capture, conflict guards, and recovery."""

import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bb_workflow.workspace import (
    WorkspaceError,
    _file_hash,
    apply_run,
    changed_paths,
    copy_snapshot,
    manifest,
    project_lock,
    snapshot_id,
    write_json,
)


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name).resolve() / "repo"
        self.repo.mkdir()
        (self.repo / "dirty.txt").write_text("user's existing uncommitted edit")
        (self.repo / "old.txt").write_text("remove me")

    def create_symlink(self, link, target, *, target_is_directory=False):
        try:
            link.symlink_to(target, target_is_directory=target_is_directory)
        except OSError as exc:
            if os.name == "nt" and (
                isinstance(exc, PermissionError) or getattr(exc, "winerror", None) == 1314
            ):
                self.skipTest("Windows symlink privilege is unavailable")
            raise

    def make_run(self):
        self.run = self.repo / ".bb-workflow" / "runs" / "example"
        self.run.mkdir(parents=True)
        self.workspace = self.run / "workspace"
        baseline = copy_snapshot(self.repo, self.workspace)
        (self.workspace / "old.txt").unlink()
        (self.workspace / "new").mkdir()
        (self.workspace / "new" / "file.txt").write_text("added")
        self.save_candidate(baseline)
        return baseline

    def save_candidate(self, baseline=None):
        if baseline is None:
            baseline = json.loads((self.run / "baseline_manifest.json").read_text())
        candidate = manifest(self.workspace)
        write_json(self.run / "baseline_manifest.json", baseline)
        write_json(self.run / "candidate_manifest.json", candidate)
        write_json(
            self.run / "state.json",
            {
                "status": "succeeded",
                "repo": str(self.repo),
                "exclude": [],
                "baseline_id": snapshot_id(baseline),
                "candidate_id": snapshot_id(candidate),
            },
        )

    def test_unknown_or_contradictory_report_contract_rejects_before_backup(self):
        self.make_run()
        path = self.run / "state.json"
        original = json.loads(path.read_text())
        for changes in (
            {"version": 7},
            {"version": True},
            {"version": "1"},
            {"version": None},
            {"version": 2},
            {"report_contract": 1},
            {"version": 1, "acceptance_report": None},
        ):
            with self.subTest(changes=changes):
                write_json(path, {**original, **changes})
                with self.assertRaises(WorkspaceError):
                    apply_run(self.run)
                self.assertFalse((self.run / "apply-backup").exists())
                self.assertEqual((self.repo / "old.txt").read_text(), "remove me")
        write_json(path, original)
        write_json(self.run / "acceptance.json", {})
        with self.assertRaises(WorkspaceError):
            apply_run(self.run)

    def test_explicit_historical_version1_still_applies_without_new_indicators(self):
        self.make_run()
        path = self.run / "state.json"
        state = json.loads(path.read_text())
        write_json(path, {**state, "version": 1})
        self.assertEqual(apply_run(self.run), ["new/file.txt", "old.txt"])

    def test_copy_manifest_preserves_initial_dirty_contents_and_permissions(self):
        (self.repo / "dirty.txt").chmod(0o755)
        destination = Path(self.temporary.name) / "snapshot"
        before = manifest(self.repo)
        self.assertEqual(copy_snapshot(self.repo, destination), before)
        self.assertEqual(manifest(destination), before)
        self.assertEqual(
            (destination / "dirty.txt").read_text(), "user's existing uncommitted edit"
        )
        self.assertNotEqual(
            (self.repo / "dirty.txt").stat().st_ino, (destination / "dirty.txt").stat().st_ino
        )

    def test_snapshot_preserves_empty_directories_and_excludes_pruned_subtrees(self):
        (self.repo / "fixtures" / "empty" / "nested").mkdir(parents=True)
        (self.repo / "target" / "empty").mkdir(parents=True)
        (self.repo / "skip-me" / "empty").mkdir(parents=True)
        destination = Path(self.temporary.name) / "snapshot"
        before = manifest(self.repo, ("skip-me",))
        self.assertEqual(copy_snapshot(self.repo, destination, ("skip-me",)), before)
        self.assertTrue((destination / "fixtures" / "empty" / "nested").is_dir())
        self.assertFalse((destination / "target").exists())
        self.assertFalse((destination / "skip-me").exists())
        self.assertEqual(manifest(destination), before)

    def test_source_empty_directory_change_during_copy_aborts(self):
        from bb_workflow import workspace

        original = workspace.shutil.copy2

        def changing_copy(source, destination, **kwargs):
            result = original(source, destination, **kwargs)
            (self.repo / "new-empty-directory").mkdir(exist_ok=True)
            return result

        with patch("bb_workflow.workspace.shutil.copy2", side_effect=changing_copy):
            with self.assertRaisesRegex(WorkspaceError, "source changed"):
                copy_snapshot(self.repo, Path(self.temporary.name) / "snapshot")

    def test_snapshot_rejects_extra_empty_destination_directory(self):
        from bb_workflow import workspace

        original = workspace.shutil.copy2
        destination_root = Path(self.temporary.name) / "snapshot"

        def changing_copy(source, destination, **kwargs):
            result = original(source, destination, **kwargs)
            (destination_root / "extra-empty-directory").mkdir(exist_ok=True)
            return result

        with patch("bb_workflow.workspace.shutil.copy2", side_effect=changing_copy):
            with self.assertRaisesRegex(WorkspaceError, "snapshot differs"):
                copy_snapshot(self.repo, destination_root)

    def test_excludes_cache_and_config_globs(self):
        for name in ("target", ".git", "thing.egg-info", ".claude"):
            (self.repo / name).mkdir()
            (self.repo / name / "ignored").write_text("ignored")
        (self.repo / "skip.log").write_text("ignored")
        self.assertEqual(set(manifest(self.repo, (".claude", "*.log"))), {"dirty.txt", "old.txt"})

    def test_default_build_exclusions_are_scoped_to_snapshot_root(self):
        for directory in ("src/build", "src/target", "src/dist", "target", "build", "dist"):
            path = self.repo / directory
            path.mkdir(parents=True)
            (path / "mod.rs").write_text("pub fn value() -> u8 { 1 }\n")
        destination = Path(self.temporary.name) / "snapshot"
        copied = copy_snapshot(self.repo, destination)
        self.assertEqual(
            set(copied),
            {"dirty.txt", "old.txt", "src/build/mod.rs", "src/target/mod.rs", "src/dist/mod.rs"},
        )
        self.assertFalse((destination / "target").exists())

    def test_ordered_reinclusions_override_cache_defaults(self):
        (self.repo / "build").mkdir()
        (self.repo / "build" / "source.rs").write_text("include me")
        (self.repo / "build" / "temporary.log").write_text("exclude me")
        destination = Path(self.temporary.name) / "snapshot"
        copied = copy_snapshot(self.repo, destination, ("!/build", "*.log"))
        self.assertIn("build/source.rs", copied)
        self.assertNotIn("build/temporary.log", copied)
        self.assertNotIn("build/source.rs", manifest(self.repo, ("!/build", "/build")))

    def test_reinclusion_can_reach_a_descendant_of_an_excluded_directory(self):
        path = self.repo / "build" / "sources"
        path.mkdir(parents=True)
        (path / "keep.rs").write_text("include me")
        (self.repo / "build" / "temporary.log").write_text("exclude me")
        destination = Path(self.temporary.name) / "snapshot"
        copied = copy_snapshot(self.repo, destination, ("!/build/sources",))
        self.assertIn("build/sources/keep.rs", copied)
        self.assertNotIn("build/temporary.log", copied)

    def test_mandatory_metadata_cannot_be_reincluded(self):
        for name in (".git", ".bb-workflow", ".code-review-graph"):
            path = self.repo / "nested" / name
            path.mkdir(parents=True)
            (path / "metadata").write_text("never copy")
        copied = copy_snapshot(
            self.repo, Path(self.temporary.name) / "snapshot", ("!*", "!/.git", "!.bb-workflow")
        )
        self.assertEqual(set(copied), {"dirty.txt", "old.txt"})

    def test_symlink_files_and_directories_require_explicit_exclude(self):
        for is_directory in (False, True):
            with self.subTest(directory=is_directory):
                link = self.repo / "link"
                self.create_symlink(
                    link,
                    self.repo if is_directory else self.repo / "dirty.txt",
                    target_is_directory=is_directory,
                )
                try:
                    with self.assertRaisesRegex(WorkspaceError, "symlink or special"):
                        manifest(self.repo)
                    self.assertNotIn("link", manifest(self.repo, ("link",)))
                finally:
                    link.unlink()

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO")
    def test_special_file_refused_without_opening(self):
        os.mkfifo(self.repo / "pipe")
        with self.assertRaisesRegex(WorkspaceError, "symlink or special"):
            manifest(self.repo)

    def test_source_change_during_copy_aborts(self):
        from bb_workflow import workspace

        original = workspace.shutil.copy2

        def changing_copy(source, destination, **kwargs):
            result = original(source, destination, **kwargs)
            (self.repo / "dirty.txt").write_text("concurrent change")
            return result

        with patch("bb_workflow.workspace.shutil.copy2", side_effect=changing_copy):
            with self.assertRaisesRegex(WorkspaceError, "source changed"):
                copy_snapshot(self.repo, Path(self.temporary.name) / "snapshot")

    def test_lock_is_exclusive_and_cleans_up(self):
        with project_lock(self.repo):
            with self.assertRaisesRegex(WorkspaceError, "remove it manually"):
                with project_lock(self.repo):
                    self.fail("second lock acquired")
        self.assertFalse((self.repo / ".bb-workflow" / "lock").exists())
        with self.assertRaisesRegex(RuntimeError, "fixture"):
            with project_lock(self.repo):
                raise RuntimeError("fixture")
        self.assertFalse((self.repo / ".bb-workflow" / "lock").exists())

    def test_apply_adds_deletes_and_preserves_user_dirty_file(self):
        self.make_run()
        paths = apply_run(self.run)
        self.assertEqual(paths, ["new/file.txt", "old.txt"])
        self.assertEqual((self.repo / "dirty.txt").read_text(), "user's existing uncommitted edit")
        self.assertEqual((self.repo / "new" / "file.txt").read_text(), "added")
        self.assertFalse((self.repo / "old.txt").exists())
        self.assertEqual((self.run / "apply-backup" / "old.txt").read_text(), "remove me")
        self.assertEqual(json.loads((self.run / "state.json").read_text())["status"], "applied")
        with self.assertRaisesRegex(WorkspaceError, "succeeded"):
            apply_run(self.run)

    def test_concurrent_original_change_prevents_every_apply_mutation(self):
        self.make_run()
        (self.repo / "dirty.txt").write_text("new user edit")
        before = manifest(self.repo)
        with self.assertRaisesRegex(WorkspaceError, "original repository changed"):
            apply_run(self.run)
        self.assertEqual(manifest(self.repo), before)
        self.assertFalse((self.run / "apply-backup").exists())

    def test_candidate_tamper_prevents_apply(self):
        self.make_run()
        (self.workspace / "dirty.txt").write_text("after validation")
        before = manifest(self.repo)
        with self.assertRaisesRegex(WorkspaceError, "candidate workspace changed"):
            apply_run(self.run)
        self.assertEqual(manifest(self.repo), before)

    def test_manifest_tamper_and_path_traversal_refused(self):
        self.make_run()
        for candidate in (
            {"../escape": "a" * 64},
            {"/escape": "a" * 64},
            {"C:\\escape": "a" * 64},
            {"safe": "a" * 64},
        ):
            with self.subTest(candidate=candidate):
                write_json(self.run / "candidate_manifest.json", candidate)
                with self.assertRaises(WorkspaceError):
                    apply_run(self.run)
        self.assertFalse((self.repo.parent / "escape").exists())

    def test_state_repo_redirect_refused(self):
        self.make_run()
        state = json.loads((self.run / "state.json").read_text())
        state["repo"] = str(self.repo.parent)
        write_json(self.run / "state.json", state)
        with self.assertRaisesRegex(WorkspaceError, "recorded repository"):
            apply_run(self.run)

    def test_directory_transitions_rejected_before_apply(self):
        self.make_run()
        (self.workspace / "dirty.txt").unlink()
        (self.workspace / "dirty.txt").mkdir()
        (self.workspace / "dirty.txt" / "child").write_text("transition")
        self.save_candidate()
        before = manifest(self.repo)
        with self.assertRaisesRegex(WorkspaceError, "transition is unsupported"):
            apply_run(self.run)
        self.assertEqual(manifest(self.repo), before)

    def test_failed_apply_rolls_back_files_and_created_directories(self):
        self.make_run()
        from bb_workflow import workspace

        original = workspace._replace_file
        calls = 0

        def failing_replace(source, target, completed=None, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                original(source, target, completed, **kwargs)
                raise OSError("injected write failure")
            return original(source, target, completed, **kwargs)

        before = manifest(self.repo)
        with patch("bb_workflow.workspace._replace_file", side_effect=failing_replace):
            with self.assertRaisesRegex(OSError, "injected write failure"):
                apply_run(self.run)
        self.assertEqual(manifest(self.repo), before)
        self.assertFalse((self.repo / "new").exists())
        report = json.loads((self.run / "apply.json").read_text())
        self.assertEqual(report["status"], "failed")
        self.assertIsNone(report["rollback_error"])

    def test_modified_existing_file_rollback_and_permissions(self):
        self.make_run()
        (self.workspace / "dirty.txt").write_text("model edit")
        self.save_candidate()
        before = manifest(self.repo)
        from bb_workflow import workspace

        original = workspace.os.replace

        def fail_second(source, target):
            if Path(target) == self.repo / "new" / "file.txt":
                raise OSError("second write fails")
            return original(source, target)

        with patch("bb_workflow.workspace.os.replace", side_effect=fail_second):
            with self.assertRaises(OSError):
                apply_run(self.run)
        self.assertEqual(manifest(self.repo), before)

    def test_candidate_changed_during_apply_copy_never_reaches_original(self):
        (self.repo / "a.txt").write_text("original a")
        self.make_run()
        (self.workspace / "a.txt").write_text("model a")
        (self.workspace / "dirty.txt").write_text("validated model change")
        self.save_candidate()
        from bb_workflow import workspace

        original_copy = workspace.shutil.copy2
        before = manifest(self.repo)

        def mutate_candidate(source, target, **kwargs):
            if Path(source) == self.workspace / "dirty.txt":
                Path(source).write_text("unvalidated concurrent candidate")
            return original_copy(source, target, **kwargs)

        with patch("bb_workflow.workspace.shutil.copy2", side_effect=mutate_candidate):
            with self.assertRaisesRegex(
                WorkspaceError, "source changed while preparing replacement"
            ):
                apply_run(self.run)
        self.assertEqual(manifest(self.repo), before)
        report = json.loads((self.run / "apply.json").read_text())
        self.assertIsNone(report["rollback_error"])
        self.assertEqual((self.repo / "a.txt").read_text(), "original a")

    def test_failed_replace_preserves_concurrent_edit_and_restores_completed_write(self):
        (self.repo / "a.txt").write_text("original a")
        self.make_run()
        (self.workspace / "a.txt").write_text("model a")
        (self.workspace / "dirty.txt").write_text("model dirty")
        self.save_candidate()
        original = os.replace

        def fail_replace(source, target):
            if Path(target) == self.repo / "dirty.txt":
                (self.repo / "dirty.txt").write_text("concurrent user edit")
                raise OSError("replacement failed before mutation")
            return original(source, target)

        with patch("bb_workflow.workspace.os.replace", side_effect=fail_replace):
            with self.assertRaisesRegex(WorkspaceError, "rollback was incomplete") as caught:
                apply_run(self.run)
        self.assertIn(str(self.run / "apply-backup"), str(caught.exception))
        self.assertEqual((self.repo / "dirty.txt").read_text(), "concurrent user edit")
        self.assertEqual((self.repo / "a.txt").read_text(), "original a")
        self.assertFalse((self.repo / "new").exists())
        report = json.loads((self.run / "apply.json").read_text())
        self.assertIn("preserved concurrent change: dirty.txt", report["rollback_error"])
        self.assertEqual(report["backup"], str(self.run / "apply-backup"))

    def test_final_verification_preserves_edit_after_successful_replace(self):
        self.make_run()
        (self.workspace / "dirty.txt").write_text("model dirty")
        self.save_candidate()
        original = manifest

        def edit_before_verification(root, exclude=()):
            if Path(root) == self.repo and not (self.repo / "old.txt").exists():
                (self.repo / "dirty.txt").write_text("new user edit after replacement")
            return original(root, exclude)

        with patch("bb_workflow.workspace.manifest", side_effect=edit_before_verification):
            with self.assertRaisesRegex(WorkspaceError, "rollback was incomplete"):
                apply_run(self.run)
        self.assertEqual((self.repo / "dirty.txt").read_text(), "new user edit after replacement")
        self.assertEqual((self.repo / "old.txt").read_text(), "remove me")
        self.assertFalse((self.repo / "new").exists())

    def test_apply_rechecks_later_path_after_preceding_successful_write(self):
        (self.repo / "a.txt").write_text("original a")
        self.make_run()
        (self.workspace / "a.txt").write_text("model a")
        (self.workspace / "dirty.txt").write_text("model dirty")
        self.save_candidate()
        original = os.replace

        def edit_next_path(source, target):
            result = original(source, target)
            if Path(target) == self.repo / "a.txt":
                (self.repo / "dirty.txt").write_text("user edit while applying earlier path")
            return result

        with patch("bb_workflow.workspace.os.replace", side_effect=edit_next_path):
            with self.assertRaisesRegex(WorkspaceError, "rollback was incomplete") as caught:
                apply_run(self.run)
        self.assertIn(
            "repository changed before applying: dirty.txt", str(caught.exception.__cause__)
        )
        self.assertEqual(
            (self.repo / "dirty.txt").read_text(), "user edit while applying earlier path"
        )
        self.assertEqual((self.repo / "a.txt").read_text(), "original a")
        self.assertEqual((self.repo / "old.txt").read_text(), "remove me")
        self.assertFalse((self.repo / "new").exists())

    def test_rollback_preserves_concurrent_recreation_after_delete(self):
        self.make_run()
        (self.workspace / "dirty.txt").write_text("model dirty")
        self.save_candidate()
        original = Path.unlink

        def recreate_after_delete(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path == self.repo / "old.txt":
                path.write_text("user recreated file")
            return result

        with patch("pathlib.Path.unlink", new=recreate_after_delete):
            with self.assertRaisesRegex(WorkspaceError, "preserved concurrent change: old.txt"):
                apply_run(self.run)
        self.assertEqual((self.repo / "old.txt").read_text(), "user recreated file")
        self.assertEqual((self.repo / "dirty.txt").read_text(), "user's existing uncommitted edit")
        self.assertFalse((self.repo / "new").exists())

    def test_interrupt_rolls_back_preceding_successful_replacement(self):
        self.make_run()
        (self.workspace / "dirty.txt").write_text("model dirty")
        self.save_candidate()
        before = manifest(self.repo)
        original = os.replace

        def interrupt_replace(source, target):
            if Path(target) == self.repo / "new" / "file.txt":
                raise KeyboardInterrupt("interrupted before replacement")
            return original(source, target)

        with patch("bb_workflow.workspace.os.replace", side_effect=interrupt_replace):
            with self.assertRaises(KeyboardInterrupt):
                apply_run(self.run)
        self.assertEqual(manifest(self.repo), before)
        self.assertFalse((self.repo / "new").exists())

    def test_rollback_keeps_replaced_file_with_identical_contents(self):
        self.make_run()
        from bb_workflow import workspace

        original = workspace.write_json

        def fail_report(path, data):
            if path == self.run / "apply.json" and data["status"] == "applied":
                replacement = self.repo / "user-copy"
                replacement.write_text("added")
                replacement.replace(self.repo / "new" / "file.txt")
                raise OSError("report write failed")
            return original(path, data)

        with patch("bb_workflow.workspace.write_json", side_effect=fail_report):
            with self.assertRaisesRegex(
                WorkspaceError, "preserved concurrent change: new/file.txt"
            ):
                apply_run(self.run)
        self.assertEqual((self.repo / "new" / "file.txt").read_text(), "added")
        self.assertEqual((self.repo / "old.txt").read_text(), "remove me")

    def test_directory_to_file_transition_is_rejected_before_mutation(self):
        (self.repo / "folder").mkdir()
        (self.repo / "folder" / "existing").write_text("preserve")
        self.make_run()
        (self.workspace / "folder" / "existing").unlink()
        (self.workspace / "folder").rmdir()
        (self.workspace / "folder").write_text("replacement")
        self.save_candidate()
        before = manifest(self.repo)
        with self.assertRaisesRegex(WorkspaceError, "transition is unsupported"):
            apply_run(self.run)
        self.assertEqual(manifest(self.repo), before)

    def test_metadata_symlink_is_refused(self):
        self.make_run()
        metadata = self.run / "candidate_manifest.json"
        original = self.run / "original-manifest.json"
        metadata.rename(original)
        self.create_symlink(metadata, original)
        with self.assertRaisesRegex(WorkspaceError, "symlink or special"):
            apply_run(self.run)

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_permission_change_invalidates_original_snapshot(self):
        self.make_run()
        (self.repo / "dirty.txt").chmod(0o755)
        with self.assertRaisesRegex(WorkspaceError, "original repository changed"):
            apply_run(self.run)

    def test_apply_does_not_write_through_existing_hardlinks(self):
        outside = Path(self.temporary.name) / "hardlink"
        os.link(self.repo / "dirty.txt", outside)
        self.make_run()
        (self.workspace / "dirty.txt").write_text("model change")
        self.save_candidate()
        apply_run(self.run)
        self.assertEqual((self.repo / "dirty.txt").read_text(), "model change")
        self.assertEqual(outside.read_text(), "user's existing uncommitted edit")

    def test_ids_and_changes_are_deterministic(self):
        self.assertEqual(snapshot_id({"b": "2", "a": "1"}), snapshot_id({"a": "1", "b": "2"}))
        self.assertEqual(changed_paths({"a": "1", "b": "2"}, {"a": "3", "c": "4"}), ["a", "b", "c"])


class FileHashTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name).resolve() / "source.txt"
        self.path.write_bytes(b"unchanged source")

    @staticmethod
    def stat_with(info, **changes):
        return SimpleNamespace(
            **{name: getattr(info, name) for name in dir(info) if name.startswith("st_")} | changes
        )

    def test_stable_file_allows_different_path_and_handle_ctime(self):
        # Windows lstat reports creation time while fstat can report change time.
        expected = hashlib.sha256(
            f"mode:{stat.S_IMODE(self.path.stat().st_mode):o}\0".encode("ascii")
            + b"unchanged source"
        ).hexdigest()
        original_fstat = os.fstat

        def handle_stat(descriptor):
            info = original_fstat(descriptor)
            return self.stat_with(info, st_ctime_ns=info.st_ctime_ns + 1_000_000_000)

        with patch("bb_workflow.workspace.os.fstat", side_effect=handle_stat):
            self.assertEqual(_file_hash(self.path), expected)

    def test_handle_ctime_change_is_rejected_even_when_path_metadata_is_stable(self):
        opened = self.path.stat()
        changed = self.stat_with(opened, st_ctime_ns=opened.st_ctime_ns + 1)
        with patch("bb_workflow.workspace.os.fstat", side_effect=[opened, changed]):
            with self.assertRaisesRegex(WorkspaceError, "file changed while reading"):
                _file_hash(self.path)

    def test_path_and_handle_content_metadata_must_agree(self):
        before = self.path.stat()
        for field in ("st_size", "st_mtime_ns"):
            with self.subTest(field=field):
                different = self.stat_with(before, **{field: getattr(before, field) + 1})
                with patch("bb_workflow.workspace.os.fstat", return_value=different):
                    with self.assertRaisesRegex(WorkspaceError, "file changed while reading"):
                        _file_hash(self.path)

    def test_path_and_handle_permissions_must_agree(self):
        before = self.path.stat()
        different = self.stat_with(before, st_mode=before.st_mode & ~stat.S_IWUSR)
        with patch("bb_workflow.workspace.os.fstat", return_value=different):
            with self.assertRaisesRegex(WorkspaceError, "file changed while reading"):
                _file_hash(self.path)

    def test_windows_path_inferred_execute_bits_allow_stable_executable(self):
        executable = self.path.with_suffix(".exe")
        self.path.rename(executable)
        info = executable.stat()
        by_path = self.stat_with(info, st_mode=stat.S_IFREG | 0o755)
        by_handle = self.stat_with(info, st_mode=stat.S_IFREG | 0o644)
        expected = hashlib.sha256(b"mode:755\0unchanged source").hexdigest()
        with (
            patch("bb_workflow.workspace.os.name", "nt"),
            patch("pathlib.Path.lstat", return_value=by_path),
            patch("bb_workflow.workspace.os.fstat", return_value=by_handle),
        ):
            self.assertEqual(_file_hash(executable), expected)

    def test_path_ctime_change_is_rejected_even_when_handle_metadata_is_stable(self):
        before = self.path.stat()
        changed = self.stat_with(before, st_ctime_ns=before.st_ctime_ns + 1)
        with (
            patch("pathlib.Path.lstat", side_effect=[before, changed]),
            patch("bb_workflow.workspace.os.fstat", return_value=before),
        ):
            with self.assertRaisesRegex(WorkspaceError, "file changed while reading"):
                _file_hash(self.path)

    def test_file_replaced_between_path_check_and_open_is_rejected(self):
        replacement = self.path.with_name("replacement.txt")
        replacement.write_bytes(b"unchanged source")
        original_open = os.open

        def replace_before_open(path, flags):
            replacement.replace(path)
            return original_open(path, flags)

        with patch("bb_workflow.workspace.os.open", side_effect=replace_before_open):
            with self.assertRaisesRegex(WorkspaceError, "file changed while reading"):
                _file_hash(self.path)

    def test_file_replaced_after_read_with_identical_contents_is_rejected(self):
        replacement = self.path.with_name("replacement.txt")
        replacement.write_bytes(b"unchanged source")
        original_lstat = Path.lstat
        calls = 0

        def replace_after_read(path):
            nonlocal calls
            calls += 1
            if calls == 2:
                replacement.replace(path)
            return original_lstat(path)

        with patch("pathlib.Path.lstat", new=replace_after_read):
            with self.assertRaisesRegex(WorkspaceError, "file changed while reading"):
                _file_hash(self.path)

    def test_content_changed_during_read_is_rejected(self):
        original_fstat = os.fstat
        calls = 0

        def change_content(descriptor):
            nonlocal calls
            info = original_fstat(descriptor)
            calls += 1
            if calls == 1:
                self.path.write_bytes(b"concurrent source edit")
            return info

        with patch("bb_workflow.workspace.os.fstat", side_effect=change_content):
            with self.assertRaisesRegex(WorkspaceError, "file changed while reading"):
                _file_hash(self.path)

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_permission_changed_during_read_is_rejected(self):
        original_fstat = os.fstat
        calls = 0

        def change_permissions(descriptor):
            nonlocal calls
            info = original_fstat(descriptor)
            calls += 1
            if calls == 1:
                self.path.chmod(0o755)
            return info

        with patch("bb_workflow.workspace.os.fstat", side_effect=change_permissions):
            with self.assertRaisesRegex(WorkspaceError, "file changed while reading"):
                _file_hash(self.path)


if __name__ == "__main__":
    unittest.main()
