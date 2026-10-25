"""Real Git repositories verify independent snapshot boundaries and baselines."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bb_workflow.process import ProcessResult, run_process
from bb_workflow.workspace import (
    WorkspaceError,
    copy_snapshot,
    initialize_git_snapshot,
    manifest,
    snapshot_environment,
)


class SnapshotEnvironmentTests(unittest.TestCase):
    def test_redirects_and_config_injection_are_removed_without_mutating_environment(self):
        injected = {
            "GIT_DIR": "/another/repository/.git",
            "GIT_COMMON_DIR": "/another/repository/.git",
            "GIT_WORK_TREE": "/another/repository",
            "GIT_INDEX_FILE": "/another/index",
            "GIT_OBJECT_DIRECTORY": "/another/objects",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/another/alternate",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.worktree",
            "GIT_CONFIG_VALUE_0": "/another/repository",
            "GIT_CONFIG_PARAMETERS": "'core.bare=true'",
            "GIT_CONFIG_GLOBAL": "/another/config",
            "GIT_SSH_COMMAND": "ssh -F some-config",
            "BB_KEEP": "value",
        }
        with patch.dict(os.environ, injected):
            result = snapshot_environment()
            self.assertEqual(os.environ["GIT_DIR"], injected["GIT_DIR"])
        for key in injected:
            if key not in {"GIT_SSH_COMMAND", "BB_KEEP"}:
                self.assertNotIn(key, result)
        self.assertEqual(result["GIT_SSH_COMMAND"], injected["GIT_SSH_COMMAND"])
        self.assertEqual(result["BB_KEEP"], "value")

    def test_missing_git_reports_actionable_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("bb_workflow.workspace.run_process", side_effect=FileNotFoundError):
                with self.assertRaisesRegex(WorkspaceError, "Git is required"):
                    initialize_git_snapshot(Path(directory))

    def test_git_timeout_and_failure_are_reported(self):
        for result, message in (
            (ProcessResult(1, "", "bad Git configuration", 0), "bad Git configuration"),
            (ProcessResult(-9, "", "", 0, timed_out=True), "timed out"),
            (ProcessResult(0, "", "", 0, output_limit_exceeded=True), "output byte limit"),
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                with patch("bb_workflow.workspace.run_process", return_value=result):
                    with self.assertRaisesRegex(WorkspaceError, message):
                        initialize_git_snapshot(Path(directory))


@unittest.skipUnless(shutil.which("git"), "Git is unavailable")
class GitSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.environment = snapshot_environment()
        self.environment.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "test@example.invalid",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "test@example.invalid",
            }
        )

    def git(self, root, *arguments):
        result = run_process(["git", *arguments], root, 5, env=self.environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_nested_snapshot_has_independent_baseline_including_dirty_and_ignored_sources(self):
        self.git(self.source, "init", "--quiet", "--template=", "--initial-branch=source")
        (self.source / "tracked.txt").write_text("original commit\n")
        (self.source / ".gitignore").write_text("ignored.txt\n.bb-workflow/\n")
        self.git(self.source, "add", ".")
        self.git(self.source, "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "source")
        (self.source / "tracked.txt").write_text("existing dirty change\n")
        (self.source / "untracked.txt").write_text("existing untracked source\n")
        (self.source / "ignored.txt").write_text("ignored source, still captured\n")
        source_before = manifest(self.source)
        status_before = self.git(self.source, "status", "--porcelain", "--untracked-files=all")
        metadata = self.source / ".git"
        metadata_before = {
            path.relative_to(metadata): path.read_bytes()
            for path in metadata.rglob("*")
            if path.is_file()
        }
        snapshot = self.source / ".bb-workflow" / "runs" / "example" / "workspace"
        copy_snapshot(self.source, snapshot)
        with patch.dict(
            os.environ,
            {
                "GIT_DIR": str(metadata),
                "GIT_WORK_TREE": str(self.source),
                "GIT_INDEX_FILE": str(metadata / "index"),
                "GIT_OBJECT_DIRECTORY": str(metadata / "objects"),
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.bare",
                "GIT_CONFIG_VALUE_0": "true",
            },
        ):
            initialize_git_snapshot(snapshot)
        self.assertEqual(Path(self.git(snapshot, "rev-parse", "--show-toplevel").strip()), snapshot)
        self.assertEqual(self.git(snapshot, "diff", "HEAD", "--"), "")
        for name in ("tracked.txt", "untracked.txt", "ignored.txt"):
            self.assertEqual(
                self.git(snapshot, "show", f"HEAD:{name}").encode("utf-8"),
                (self.source / name).read_bytes(),
            )
        (snapshot / "tracked.txt").write_text("workflow change\n")
        difference = self.git(snapshot, "diff", "HEAD", "--", "tracked.txt")
        self.assertIn("-existing dirty change", difference)
        self.assertIn("+workflow change", difference)
        self.assertEqual(manifest(self.source), source_before)
        self.assertEqual(
            self.git(self.source, "status", "--porcelain", "--untracked-files=all"), status_before
        )
        self.assertEqual(
            {
                path.relative_to(metadata): path.read_bytes()
                for path in metadata.rglob("*")
                if path.is_file()
            },
            metadata_before,
        )

    def test_global_hooks_signing_templates_and_attributes_do_not_change_snapshot_bytes(self):
        snapshot = self.root / "snapshot"
        snapshot.mkdir()
        (snapshot / ".gitattributes").write_text("*.txt text eol=lf filter=must-not-run ident\n")
        captured = b"captured\r\n$Id$\r\n"
        (snapshot / "file.txt").write_bytes(captured)
        (snapshot / "file.txt").chmod(0o755)
        before = manifest(snapshot)
        config = self.root / "global.gitconfig"
        config.write_text(
            '[commit]\n gpgSign = true\n[filter "must-not-run"]\n clean = false\n required = true\n'
        )
        template = self.root / "template"
        (template / "hooks").mkdir(parents=True)
        hook = template / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        with patch.dict(
            os.environ, {"GIT_CONFIG_GLOBAL": str(config), "GIT_TEMPLATE_DIR": str(template)}
        ):
            initialize_git_snapshot(snapshot)
        self.assertFalse((snapshot / ".git" / "hooks" / "pre-commit").exists())
        self.assertEqual(manifest(snapshot), before)
        self.assertEqual((snapshot / "file.txt").read_bytes(), captured)
        self.assertEqual(self.git(snapshot, "show", "HEAD:file.txt").encode(), captured)
        self.assertEqual(self.git(snapshot, "diff", "HEAD", "--"), "")

    def test_empty_snapshot_gets_baseline_and_existing_metadata_is_never_reinitialized(self):
        initialize_git_snapshot(self.source)
        head = self.git(self.source, "rev-parse", "HEAD")
        with self.assertRaisesRegex(WorkspaceError, "already has Git metadata"):
            initialize_git_snapshot(self.source)
        self.assertEqual(self.git(self.source, "rev-parse", "HEAD"), head)

    @unittest.skipUnless(shutil.which("cargo"), "Cargo is unavailable")
    def test_snapshot_keeps_rust_modules_named_build_target_and_dist(self):
        (self.source / "Cargo.toml").write_text(
            '[package]\nname = "snapshot-module-fixture"\nversion = "0.1.0"\nedition = "2021"\n'
        )
        (self.source / "src").mkdir()
        (self.source / "src" / "lib.rs").write_text(
            "pub mod build;\npub mod target;\npub mod dist;\n"
        )
        for name in ("build", "target", "dist"):
            path = self.source / "src" / name
            path.mkdir()
            (path / "mod.rs").write_text("pub fn value() -> u8 { 1 }\n")
        snapshot = self.root / "snapshot"
        copy_snapshot(self.source, snapshot)
        initialize_git_snapshot(snapshot)
        result = run_process(
            ["cargo", "check", "--offline"], snapshot, 20, env=snapshot_environment()
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
