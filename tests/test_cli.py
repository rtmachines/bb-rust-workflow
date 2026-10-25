import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bb_workflow.cli import _doctor, main
from bb_workflow.config import LEGACY_CHECKS, Selection, default_text, load_config
from bb_workflow.process import ProcessResult
from bb_workflow.verification import verify_repository


class CliTests(unittest.TestCase):
    def test_verify_uses_discovered_config_and_reports_without_provider_calls(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            command = [sys.executable, "-c", "from pathlib import Path; print(Path.cwd())"]
            (repo / "workflow.toml").write_text(
                default_text()
                + "\n[verification.profiles.fast]\nchecks = "
                + json.dumps([command]),
                encoding="utf-8",
            )
            stream = io.StringIO()
            with (
                contextlib.redirect_stdout(stream),
                patch("bb_workflow.cli.probe") as probe,
                patch("bb_workflow.cli.Workflow") as workflow,
            ):
                code = main(["verify", "fast", "--repo", str(repo)])
            self.assertEqual(code, 0)
            payload = json.loads(stream.getvalue())
            self.assertEqual(payload["status"], "passed")
            directory = Path(payload["run_dir"])
            report = json.loads((directory / "report.json").read_text())
            self.assertEqual(report["plan"]["profile"], "fast")
            self.assertFalse(report["apply_eligible"])
            self.assertIn(str(repo), report["results"][0]["stdout_tail"])
            self.assertFalse((directory / "workspace").exists())
            probe.assert_not_called()
            workflow.assert_not_called()

    def test_verify_gate_narrowing_is_configuration_error_without_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            for scope in (["--package", "one"], ["--test-filter", "unit"]):
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main(["verify", "gate", "--repo", str(repo), *scope]), 2)
            self.assertFalse((repo / ".bb-workflow").exists())

    def test_verify_maps_failure_blocked_and_interrupt_exit_codes(self):
        cases = [
            (ProcessResult(1, "", "bad", 0), 1, "failed"),
            (FileNotFoundError("gone"), 1, "blocked"),
            (KeyboardInterrupt(), 130, "interrupted"),
        ]
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            for action, expected_code, status in cases:

                def process(*args, **kwargs):
                    if isinstance(action, BaseException):
                        raise action
                    return action

                def verify(repo, config, profile, **kwargs):
                    return verify_repository(repo, config, profile, process_fn=process, **kwargs)

                stream = io.StringIO()
                with (
                    contextlib.redirect_stdout(stream),
                    patch("bb_workflow.cli.verify_repository", side_effect=verify),
                ):
                    self.assertEqual(main(["verify", "gate", "--repo", str(repo)]), expected_code)
                self.assertEqual(json.loads(stream.getvalue())["status"], status)

    def test_plan_exposes_resolved_gate_and_verification_namespace(self):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            self.assertEqual(main(["plan", "task"]), 0)
        plan = json.loads(stream.getvalue())
        self.assertEqual(set(plan["verification"]["profiles"]), {"fast", "focused", "lint", "gate"})
        self.assertEqual(plan["verification"]["gate_plan"]["profile"], "gate")
        self.assertEqual(
            [c["argv"] for c in plan["verification"]["gate_plan"]["checks"]], plan["checks"]
        )

    def test_init_refuses_to_overwrite_and_plan_needs_no_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "workflow.toml"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["init", "--config", str(path)]), 0)
            original = path.read_bytes()
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["init", "--config", str(path)]), 2)
            self.assertEqual(path.read_bytes(), original)
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                code = main(["plan", "Improve unsafe code", "--config", str(path)])
            self.assertEqual(code, 0)
            plan = json.loads(stream.getvalue())
            self.assertEqual(plan["config_version"], 2)
            self.assertFalse(plan["compatibility_mode"])
            self.assertEqual(plan["checks"], load_config(path).checks)
            self.assertEqual(plan["checks_scope"]["kind"], "workspace_all_targets_and_doctests")
            self.assertEqual(plan["complexity"], "high")
            self.assertEqual(plan["prepare"], load_config(path).prepare)
            self.assertEqual(plan["max_output_bytes"], 16 * 1024 * 1024)
            self.assertEqual(
                plan["context"],
                {
                    "max_diff_chars": 16000,
                    "failed_check_tail_chars": 2000,
                    "skill_mode": "reference",
                },
            )

    def test_plan_preserves_custom_commands_and_reports_compatibility(self):
        custom_checks = [["custom checker", "--package", "one member"], ["cargo", "test", "unit"]]
        for version in (None, 1, 2):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temporary:
                repo = Path(temporary)
                text = default_text()
                checks_start = text.index("checks = ")
                checks_end = text.index("\nexclude = ", checks_start)
                text = (
                    text[:checks_start]
                    + "checks = "
                    + json.dumps(custom_checks)
                    + text[checks_end:]
                )
                text = text.replace(
                    "config_version = 2\n",
                    "" if version is None else f"config_version = {version}\n",
                )
                (repo / "workflow.toml").write_text(text, encoding="utf-8")
                stream = io.StringIO()
                with (
                    contextlib.redirect_stdout(stream),
                    patch("bb_workflow.cli.probe") as probe,
                    patch("bb_workflow.cli.Workflow") as workflow,
                ):
                    self.assertEqual(main(["plan", "task", "--repo", str(repo)]), 0)
                plan = json.loads(stream.getvalue())
                self.assertEqual(plan["config_version"], 1 if version is None else version)
                self.assertEqual(plan["compatibility_mode"], version != 2)
                self.assertEqual(plan["checks"], custom_checks)
                self.assertEqual(plan["checks_scope"]["kind"], "configured_commands")
                probe.assert_not_called()
                workflow.assert_not_called()

    def test_plan_exposes_legacy_fallback_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.toml"
            text = default_text().replace("config_version = 2\n", "")
            start = text.index("checks = ")
            end = text.index("\nexclude = ", start)
            path.write_text(text[:start] + text[end:], encoding="utf-8")
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                self.assertEqual(main(["plan", "task", "--config", str(path)]), 0)
            plan = json.loads(stream.getvalue())
            self.assertTrue(plan["compatibility_mode"])
            self.assertEqual(plan["checks"], [list(command) for command in LEGACY_CHECKS])
            self.assertEqual(plan["checks_scope"]["kind"], "legacy_defaults")

    def test_missing_config_is_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["plan", "task", "--config", "/missing/config.toml"]), 2)

    def test_status_summary_omits_evidence_and_supports_legacy_states(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            state = {
                "status": "succeeded",
                "phase": "finished",
                "reason": "Checks passed",
                "complexity": "medium",
                "cycles": 1,
                "agent_calls": 4,
                "elapsed_seconds": 12.5,
                "known_cost_usd": 0.2,
                "unknown_cost_calls": 1,
                "checks": [{"stdout_tail": "RAW_LOG_NOT_FOR_SUMMARY"}],
                "agents": [{"summary": "Detailed agent report"}],
                "config": {"checks": [["cargo", "test"]]},
            }
            for with_tokens in (False, True):
                with self.subTest(with_tokens=with_tokens):
                    if with_tokens:
                        state["token_usage"] = {"by_backend": {"codex": {"calls": 1}}}
                    (directory / "state.json").write_text(json.dumps(state), encoding="utf-8")
                    full = io.StringIO()
                    with contextlib.redirect_stdout(full):
                        self.assertEqual(main(["status", str(directory)]), 0)
                    self.assertEqual(json.loads(full.getvalue()), state)
                    compact = io.StringIO()
                    with contextlib.redirect_stdout(compact):
                        self.assertEqual(main(["status", str(directory), "--summary"]), 0)
                    summary = json.loads(compact.getvalue())
                    self.assertEqual(summary["status"], "succeeded")
                    self.assertEqual(summary["agent_calls"], 4)
                    self.assertEqual(summary["known_cost_usd"], 0.2)
                    self.assertEqual(summary["unknown_cost_calls"], 1)
                    self.assertEqual(summary["run_dir"], str(directory.resolve()))
                    self.assertEqual(summary["report"], str(directory.resolve() / "report.md"))
                    self.assertNotIn("RAW_LOG_NOT_FOR_SUMMARY", compact.getvalue())
                    for key in ("checks", "agents", "config"):
                        self.assertNotIn(key, summary)
                    if with_tokens:
                        self.assertEqual(summary["token_usage"], state["token_usage"])
                    else:
                        self.assertNotIn("token_usage", summary)

    def test_doctor_uses_target_directory_and_only_selected_backends(self):
        config = load_config()
        config.checks = [["./scripts/check", "verify"]]
        config.roles = {
            role: Selection("codex", "gpt-6-luna", "low") for role in config.profiles["low"]
        }
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            with patch("bb_workflow.cli.probe", return_value={"supported": True}) as probe:
                with patch(
                    "bb_workflow.cli.run_process", return_value=ProcessResult(0, "v1", "", 0)
                ) as run:
                    _, ok = _doctor(config, repo)
            self.assertTrue(ok)
            self.assertEqual(probe.call_count, 1)
            self.assertEqual(probe.call_args.args, ("codex", "codex"))
            self.assertEqual(probe.call_args.kwargs["cwd"], repo.resolve())
            self.assertEqual(run.call_args.kwargs["cwd"], repo.resolve())

    def test_doctor_checks_git_and_deduplicated_prepare_and_validation_executables(self):
        config = load_config()
        config.prepare = [["./scripts/prepare", "generate"], ["cargo", "fetch"]]
        config.checks = [["cargo", "check"], ["cargo", "test"]]
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            with (
                patch("bb_workflow.cli.probe", return_value={"supported": True}),
                patch(
                    "bb_workflow.cli.run_process", return_value=ProcessResult(0, "v1", "", 0)
                ) as run,
                patch.dict(os.environ, {"GIT_DIR": "/outside/.git"}),
            ):
                report, ok = _doctor(config, repo)
        self.assertTrue(ok)
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [["git", "--version"], ["./scripts/prepare", "--version"], ["cargo", "--version"]],
        )
        self.assertTrue(report["git"]["available"])
        self.assertEqual(set(report["preparation_executables"]), {"./scripts/prepare", "cargo"})
        self.assertEqual(set(report["validation_executables"]), {"cargo"})
        for call in run.call_args_list:
            self.assertNotIn("GIT_DIR", call.kwargs["env"])
            self.assertEqual(call.kwargs["cwd"], repo.resolve())

    def test_doctor_fails_on_missing_git_prepare_or_output_overflow(self):
        config = load_config()
        config.prepare = [["prepare-tool", "generate"]]
        config.checks = [["check-tool", "verify"]]
        failures = (
            ("git", FileNotFoundError("git unavailable")),
            ("prepare-tool", FileNotFoundError("prepare unavailable")),
            ("check-tool", ProcessResult(0, "v1", "", 0, output_limit_exceeded=True)),
        )
        for executable, failure in failures:

            def fake(argv, **kwargs):
                if argv[0] == executable:
                    if isinstance(failure, Exception):
                        raise failure
                    return failure
                return ProcessResult(0, "v1", "", 0)

            with (
                self.subTest(executable=executable),
                patch("bb_workflow.cli.probe", return_value={"supported": True}),
                patch("bb_workflow.cli.run_process", side_effect=fake),
            ):
                _, ok = _doctor(config, Path.cwd())
                self.assertFalse(ok)

    @unittest.skipIf(os.name == "nt", "POSIX executable fixture")
    def test_doctor_relative_provider_works_from_another_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            repo, outside = base / "repo", base / "outside"
            repo.mkdir()
            outside.mkdir()
            provider = repo / "fixture-codex"
            provider.write_text(
                '#!/bin/sh\ncase "$*" in\n'
                '  --version) echo "codex-cli 0.164.0";;\n'
                '  "exec --help") echo "--model --sandbox --skip-git-repo-check '
                "--ephemeral --json --output-schema --output-last-message "
                '--config --ignore-user-config";;\n'
                "  *) exit 9;;\nesac\n",
                encoding="utf-8",
            )
            provider.chmod(0o755)
            config = load_config()
            config.roles = {
                role: Selection("codex", "gpt-6-luna", "low") for role in config.profiles["low"]
            }
            config.executables["codex"] = "./fixture-codex"
            config.checks = [["git", "--version"]]
            with contextlib.chdir(outside):
                report, ok = _doctor(config, repo)
            self.assertTrue(ok, report)
            self.assertEqual(report["backends"]["codex"]["version"], "codex-cli 0.164.0")
