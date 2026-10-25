"""Windows reparse points cannot redirect snapshot or apply traversal."""

import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bb_workflow import workspace
from bb_workflow.process import run_process
from bb_workflow.workspace import (
    WorkspaceError,
    apply_run,
    copy_snapshot,
    manifest,
    snapshot_id,
    write_json,
)


class ReparsePointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()

    def flagged_lstat(self, flagged):
        original = Path.lstat

        def lstat(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == flagged:
                return SimpleNamespace(
                    st_mode=info.st_mode,
                    st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
                )
            return info

        return patch.object(Path, "lstat", new=lstat)

    def test_directory_and_regular_checks_reject_reparse_attributes_on_every_platform(self):
        file = self.repo / "file.txt"
        file.write_text("preserve")
        for path, check, message in (
            (self.repo, workspace._directory, "real directory"),
            (file, workspace._regular, "symlink or special"),
        ):
            with self.subTest(path=path), self.flagged_lstat(path):
                with self.assertRaisesRegex(WorkspaceError, message):
                    check(path)

    def test_scan_rejects_directory_reparse_point_before_visiting_children(self):
        redirected = self.repo / "redirected"
        redirected.mkdir()
        (redirected / "file.txt").write_text("preserve")
        original = Path.iterdir

        def iterdir(path):
            if path == redirected:
                raise AssertionError("scan followed a directory reparse point")
            return original(path)

        with self.flagged_lstat(redirected), patch.object(Path, "iterdir", new=iterdir):
            with self.assertRaisesRegex(WorkspaceError, "symlink or special"):
                copy_snapshot(self.repo, self.root / "snapshot")
            self.assertEqual(manifest(self.repo, ("redirected", "!*else*")), {})

    def test_apply_preflight_rejects_reparse_target_and_parent(self):
        folder = self.repo / "folder"
        folder.mkdir()
        file = folder / "file.txt"
        file.write_text("preserve")
        for flagged in (folder, file):
            with self.subTest(flagged=flagged), self.flagged_lstat(flagged):
                with self.assertRaisesRegex(WorkspaceError, "transition is unsupported"):
                    workspace._preflight(self.repo, ["folder/file.txt"])


@unittest.skipUnless(os.name == "nt", "native Windows junctions")
class NativeJunctionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.outside = self.root / "outside"
        self.outside.mkdir()
        (self.outside / "file.txt").write_text("outside contents")

    def junction(self, path):
        result = run_process(
            [
                os.environ.get("COMSPEC", "cmd.exe"),
                "/c",
                "mklink",
                "/J",
                str(path),
                str(self.outside),
            ],
            self.root,
            5,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.addCleanup(path.rmdir)

    def test_snapshot_rejects_native_junction_without_reading_outside(self):
        self.junction(self.repo / "folder")
        with self.assertRaisesRegex(WorkspaceError, "symlink or special"):
            copy_snapshot(self.repo, self.root / "snapshot")
        self.assertEqual((self.outside / "file.txt").read_text(), "outside contents")
        self.assertFalse((self.root / "snapshot").exists())

    def test_apply_rejects_native_junction_and_preserves_outside_file(self):
        folder = self.repo / "folder"
        folder.mkdir()
        file = folder / "file.txt"
        file.write_text("baseline")
        run = self.repo / ".bb-workflow" / "runs" / "example"
        run.mkdir(parents=True)
        baseline = copy_snapshot(self.repo, run / "workspace")
        (run / "workspace" / "folder" / "file.txt").write_text("workflow edit")
        candidate = manifest(run / "workspace")
        write_json(run / "baseline_manifest.json", baseline)
        write_json(run / "candidate_manifest.json", candidate)
        write_json(
            run / "state.json",
            {
                "status": "succeeded",
                "repo": str(self.repo),
                "exclude": [],
                "baseline_id": snapshot_id(baseline),
                "candidate_id": snapshot_id(candidate),
            },
        )
        file.unlink()
        folder.rmdir()
        self.junction(folder)
        with self.assertRaisesRegex(WorkspaceError, "symlink or special"):
            apply_run(run)
        self.assertEqual((self.outside / "file.txt").read_text(), "outside contents")
        with self.assertRaisesRegex(WorkspaceError, "transition is unsupported"):
            workspace._preflight(self.repo, ["folder/file.txt"])


if __name__ == "__main__":
    unittest.main()
