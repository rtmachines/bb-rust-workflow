import argparse
import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch

from bb_workflow import __version__
from bb_workflow.backends import BackendResult
from bb_workflow.cli import _doctor, _parser, main
from bb_workflow.config import LEGACY_CHECKS, Selection, default_text, load_config
from bb_workflow.engine import Workflow
from bb_workflow.process import ProcessResult
from bb_workflow.verification import verify_repository


class CliTests(unittest.TestCase):
    def test_all_commands_are_discoverable_and_help_has_no_side_effects(self):
        parser = _parser()
        commands = next(
            action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
        )
        expected = {"init", "plan", "run", "doctor", "verify", "status", "apply"}
        self.assertEqual(set(commands.choices), expected)
        self.assertEqual(
            {action.dest for action in commands._choices_actions if action.help}, expected
        )
        examples = (
            ["init", "--config", "new.toml"],
            ["plan", "task", "--repo", "project", "--requirements", "task.toml"],
            ["run", "task", "--complexity", "low", "--events-jsonl"],
            ["doctor", "--repo", "project"],
            ["verify", "focused", "--package", "one", "--test-filter", "unit"],
            ["status", "saved-run", "--summary"],
            ["apply", "saved-run"],
        )
        with tempfile.TemporaryDirectory() as temporary, contextlib.chdir(temporary):
            for argv in examples:
                with self.subTest(command=argv[0]):
                    self.assertEqual(parser.parse_args(argv).command, argv[0])
                    stream = io.StringIO()
                    with contextlib.redirect_stdout(stream):
                        self.assertEqual(main([argv[0], "--help", "--json"]), 0)
                    self.assertTrue(json.loads(stream.getvalue())["data"]["help"])
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_version_exits_without_command_or_project_and_preserves_json_contract(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.chdir(temporary):
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream), self.assertRaises(SystemExit) as raised:
                main(["--version"])
            self.assertEqual(raised.exception.code, 0)
            self.assertEqual(stream.getvalue().strip(), f"bb-workflow {__version__}")
            for argv in (["--json", "--version"], ["--version", "--json"]):
                with self.subTest(argv=argv):
                    stream = io.StringIO()
                    with contextlib.redirect_stdout(stream):
                        self.assertEqual(main(argv), 0)
                    self.assertEqual(
                        json.loads(stream.getvalue()),
                        {
                            "schema_version": 1,
                            "command": None,
                            "ok": True,
                            "exit_code": 0,
                            "data": {"version": __version__},
                            "error": None,
                        },
                    )
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_packaged_operator_skill_can_be_copied_without_overwriting(self):
        resource = files("bb_workflow").joinpath("skills/bb-workflow/SKILL.md")
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "chosen-skills" / "bb-workflow" / "SKILL.md"
            destination.parent.mkdir(parents=True)
            with destination.open("xb") as stream:
                stream.write(resource.read_bytes())
            self.assertGreater(destination.stat().st_size, 0)
            self.assertFalse(destination.is_symlink())
            before = destination.read_bytes()
            with self.assertRaises(FileExistsError), destination.open("xb"):
                pass
            self.assertEqual(destination.read_bytes(), before)

    def test_operator_sequence_from_external_directory_keeps_source_until_explicit_apply(self):
        """Catch path regressions and accidental apply or loss of bound independent reviews."""
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            repo, caller = base / "project", base / "caller"
            repo.mkdir()
            caller.mkdir()
            source = repo / "lib.rs"
            source.write_text("pub fn value() -> u32 { 0 }\n", encoding="utf-8")
            calls, outputs = [], []
            task = "unsafe Ω\r\n'quoted' $(touch forbidden) `cmd` ; --json\n" * 6000
            self.assertGreaterEqual(len(task.encode("utf-8")), 256 * 1024)

            def provider(**call):
                payload = json.loads(call["prompt"].split("Task and evidence (JSON):\n", 1)[1])
                self.assertEqual(payload["task"], task)
                binding = payload["acceptance"]["binding"]
                calls.append((call["role"], call["cwd"], binding))
                response = {
                    "schema_version": 2,
                    "status": "completed",
                    "summary": "Fixture inspected the requested value",
                    "findings": [],
                    "decisions": [],
                    "binding": binding,
                    "criterion_evidence": [],
                    "criterion_assessments": [],
                    "criterion_decisions": [],
                }
                if call["role"] == "implementation":
                    (call["cwd"] / "lib.rs").write_text(
                        "pub fn value() -> u32 { 1 }\n", encoding="utf-8"
                    )
                else:
                    self.assertEqual(
                        (call["cwd"] / "lib.rs").read_text(), "pub fn value() -> u32 { 1 }\n"
                    )
                    response["criterion_assessments"] = [
                        {
                            "criterion_id": "value",
                            "verdict": "satisfied",
                            "evidence": "Source inspected",
                        }
                    ]
                return BackendResult(response, None, {}, call["selection"].model)

            def workflow(*args, **kwargs):
                return Workflow(
                    *args,
                    **kwargs,
                    invoke_fn=provider,
                    process_fn=lambda *args, **kwargs: ProcessResult(0, "Fixture passed", "", 0),
                )

            def command(argv, expected=0):
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = main(["--json", *argv])
                self.assertEqual(code, expected, stderr.getvalue() + stdout.getvalue())
                outputs.append(stdout.getvalue())
                envelope = json.loads(stdout.getvalue())
                self.assertEqual(envelope["ok"], expected == 0)
                return envelope

            with contextlib.chdir(caller), patch("bb_workflow.cli.Workflow", side_effect=workflow):
                config = repo / "workflow.toml"
                command(["init", "--config", str(config)])
                text = config.read_text()
                start, end = text.index("prepare = "), text.index("\nexclude = ")
                config.write_text(
                    text[:start] + 'prepare = []\nchecks = [["fixture-check"]]' + text[end:],
                    encoding="utf-8",
                )
                Path("task.toml").write_text(
                    'requirements_version = 1\n[[criteria]]\nid = "gate"\n'
                    'description = "Configured check passes"\nkind = "mechanical"\n'
                    'verification = "Run the full gate"\ncheck_id = "0"\n'
                    '[[criteria]]\nid = "value"\ndescription = "value returns one"\n'
                    'kind = "semantic"\nverification = "Inspect source and API"\n',
                    encoding="utf-8",
                )
                Path("docs.txt").write_bytes(task.encode("utf-8"))
                arguments = [
                    "--task-file",
                    "docs.txt",
                    "--repo",
                    str(repo),
                    "--requirements",
                    "task.toml",
                ]
                plan = command(["plan", *arguments])["data"]
                self.assertFalse(plan["requirements_needed"])
                self.assertEqual(plan["complexity"], "high")
                self.assertEqual(calls, [])
                result = command(["run", *arguments, "--events-jsonl"])["data"]
                self.assertEqual(result["status"], "succeeded")
                directory = Path(result["run_dir"])
                saved = json.loads((directory / "state.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["task"], task)
                self.assertEqual(saved["complexity"], "high")
                summary = command(["status", str(directory), "--summary"])["data"]
                self.assertEqual(summary["status"], "succeeded")
                self.assertEqual(summary["acceptance"]["requirements_id"], plan["requirements_id"])
                self.assertEqual(
                    summary["acceptance"]["verification_plan_id"], plan["verification_plan_id"]
                )
                self.assertEqual(source.read_text(), "pub fn value() -> u32 { 0 }\n")
                self.assertFalse((directory / "apply.json").exists())
                self.assertEqual(
                    [call[0] for call in calls], ["implementation", "reviewer_a", "reviewer_b"]
                )
                self.assertNotEqual(calls[1][1], calls[2][1])
                self.assertEqual(calls[1][2], calls[2][2])
                acceptance = json.loads((directory / "acceptance.json").read_text())
                self.assertEqual(acceptance["binding"], calls[1][2])
                failure = command(["plan", *arguments, "--config", "missing.toml"], expected=2)
                self.assertEqual(failure["error"]["code"], "invalid_config")
                self.assertEqual(failure["error"]["next_action"], "correct_configuration")
                self.assertEqual(len(calls), 3)
                self.assertEqual(source.read_text(), "pub fn value() -> u32 { 0 }\n")
                applied = command(["apply", str(directory)])["data"]
                self.assertEqual(applied, {"status": "applied", "changed_paths": ["lib.rs"]})
                self.assertEqual(source.read_text(), "pub fn value() -> u32 { 1 }\n")
            self.operator_metrics = {
                "happy_path_commands": len(outputs) - 1,
                "planned_failure_probes": 1,
                "accidental_configuration_failures": 0,
                "stdout_chars": sum(map(len, outputs)),
                "stdout_bytes": sum(len(output.encode("utf-8")) for output in outputs),
            }

    def test_verify_uses_discovered_config_and_reports_without_provider_calls(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp).resolve()
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
            self.assertEqual(plan["config_version"], 3)
            self.assertFalse(plan["compatibility_mode"])
            self.assertTrue(plan["requirements_needed"])
            self.assertEqual(plan["acceptance_mode"], "required")
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
        for version in (None, 1, 2, 3):
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
                    "config_version = 3\n",
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
                self.assertEqual(plan["compatibility_mode"], version != 3)
                self.assertEqual(plan["checks"], custom_checks)
                self.assertEqual(plan["checks_scope"]["kind"], "configured_commands")
                probe.assert_not_called()
                workflow.assert_not_called()

    def test_plan_exposes_legacy_fallback_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.toml"
            text = default_text().replace("config_version = 3\n", "")
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

    @unittest.skipUnless(os.name == "posix", "POSIX termination")
    def test_sigterm_long_run_preserves_logs_terminates_child_and_releases_lock(self):
        for events, machine in ((False, False), (True, False), (True, True)):
            with (
                self.subTest(events=events, machine=machine),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                repo = root / "repo"
                repo.mkdir()
                (repo / "source.txt").write_text("original")
                marker = root / "child.json"
                script = (
                    "import json,os,time; from pathlib import Path; "
                    "print('fixture output', flush=True); "
                    f"Path({str(marker)!r}).write_text(json.dumps(os.getpid())); "
                    "time.sleep(60)"
                )
                config = default_text().replace("config_version = 3", "config_version = 2")
                start = config.index("checks = ")
                end = config.index("\nexclude = ", start)
                config = (
                    config[:start]
                    + "checks = "
                    + json.dumps([[sys.executable, "-c", script]])
                    + config[end:]
                )
                (repo / "workflow.toml").write_text(config)
                argv = [sys.executable, "-m", "bb_workflow.cli", "run", "task", "--repo", str(repo)]
                if events:
                    argv.append("--events-jsonl")
                if machine:
                    argv.append("--json")
                with subprocess.Popen(
                    argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env={
                        **os.environ,
                        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                    },
                ) as child:
                    try:
                        announcement = child.stderr.readline()
                        if events:
                            announcement_event = json.loads(announcement)
                            self.assertEqual(announcement_event["event"], "start")
                            directory = Path(announcement_event["run_dir"])
                        else:
                            self.assertTrue(announcement.startswith("Run directory: "))
                            directory = Path(announcement.removeprefix("Run directory: ").strip())
                        self.assertEqual(
                            json.loads((directory / "state.json").read_text())["status"], "running"
                        )
                        deadline = time.monotonic() + 5
                        while not marker.exists() and time.monotonic() < deadline:
                            time.sleep(0.01)
                        self.assertTrue(marker.exists())
                        child_pid = json.loads(marker.read_text())
                        active = json.loads((directory / "progress.json").read_text())
                        self.assertEqual(
                            active["active_operation"]["command"], [sys.executable, "-c", script]
                        )
                        child.send_signal(signal.SIGTERM)
                        stdout, stderr = child.communicate(timeout=10)
                    finally:
                        if child.poll() is None:
                            child.kill()
                            child.communicate()
                self.assertEqual(child.returncode, 130, stderr)
                payload = json.loads(stdout)
                if machine:
                    self.assertFalse(payload["ok"])
                    self.assertEqual(payload["error"]["code"], "interrupted")
                    self.assertEqual(payload["exit_code"], 130)
                    payload = payload["data"]
                self.assertEqual(payload["status"], "interrupted")
                state = json.loads((directory / "state.json").read_text())
                self.assertEqual(state["status"], "interrupted")
                self.assertTrue(state["checks"][-1]["interrupted"])
                self.assertIn(
                    "fixture output", (directory / "checks/baseline/0/stdout.txt").read_text()
                )
                self.assertIn("interrupted", (directory / "report.md").read_text())
                self.assertFalse((repo / ".bb-workflow/lock").exists())
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)
                if events:
                    emitted = [
                        announcement_event,
                        *(json.loads(line) for line in stderr.splitlines()),
                    ]
                    self.assertEqual(emitted[-1]["event"], "finish")
                    self.assertTrue(
                        all(item["version"] == 1 and "phase" in item for item in emitted)
                    )

    def test_status_summary_uses_valid_live_progress_only_while_running(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            progress = {
                "version": 1,
                "run_dir": str(directory),
                "pid": 123,
                "phase": "implementation",
                "active_operation": {"role": "implementation"},
                "updated_at": "2026-10-25T10:00:00+00:00",
                "elapsed_seconds": 5.0,
                "heartbeat_interval_seconds": 5.0,
            }
            for status, telemetry, expected in (
                ("running", json.dumps(progress), progress),
                ("succeeded", json.dumps(progress), None),
                ("running", "broken JSON", None),
                ("running", "[]", None),
                ("running", json.dumps({**progress, "run_dir": "/different/run"}), None),
                ("running", json.dumps({**progress, "elapsed_seconds": 10**400}), None),
                ("running", json.dumps({**progress, "heartbeat_interval_seconds": 0}), None),
            ):
                (directory / "state.json").write_text(json.dumps({"status": status}))
                (directory / "progress.json").write_text(telemetry)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(main(["status", str(directory), "--summary"]), 0)
                self.assertEqual(json.loads(output.getvalue()).get("progress"), expected)

    def test_outer_interrupt_returns_130_and_restores_sigterm_handler(self):
        import signal

        previous = signal.getsignal(signal.SIGTERM)
        with patch("bb_workflow.cli._config", side_effect=KeyboardInterrupt()):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["run", "task"]), 130)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

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


class TaskInputTests(unittest.TestCase):
    def invoke(self, argv):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(argv)
        return code, json.loads(output.getvalue()), errors.getvalue()

    def test_file_and_stdin_reach_routing_and_workflow_unchanged(self):
        task = "  unsafe Ω\r\n'quoted' $(touch forbidden) `cmd` ; --json\n\n"
        for command in ("plan", "run"):
            for source in ("file", "binary", "text"):
                with (
                    self.subTest(command=command, source=source),
                    tempfile.TemporaryDirectory() as temp,
                ):
                    base = Path(temp)
                    repo = base / "repo"
                    repo.mkdir()
                    (base / "docs.txt").write_bytes(task.encode("utf-8"))
                    stream = (
                        io.TextIOWrapper(io.BytesIO(task.encode("utf-8")), encoding="ascii")
                        if source == "binary"
                        else io.StringIO(task)
                    )
                    with (
                        contextlib.chdir(base),
                        patch("sys.stdin", stream),
                        patch("bb_workflow.cli.Workflow") as workflow,
                    ):
                        workflow.return_value.state = {"status": "succeeded", "reason": "fixture"}
                        workflow.return_value.run.return_value = repo / "saved"
                        code, result, _ = self.invoke(
                            [
                                command,
                                "--task-file",
                                "docs.txt" if source == "file" else "-",
                                "--repo",
                                str(repo),
                                "--json",
                            ]
                        )
                        self.assertEqual(code, 0, result)
                        if command == "plan":
                            self.assertEqual(result["data"]["complexity"], "high")
                        else:
                            self.assertEqual(workflow.call_args.args[1], task)
                        self.assertFalse(stream.closed)
                    self.assertFalse((base / "forbidden").exists())

    def test_selection_errors_and_help_do_not_read_stdin(self):
        for command in ("plan", "run"):
            for machine in (False, True):
                for selection, expected in (
                    ([], 2),
                    (["task", "--task-file", "-"], 2),
                    (["--task-file", "-", "--help"], 0),
                ):
                    with self.subTest(command=command, machine=machine, selection=selection):
                        with (
                            patch("sys.stdin") as stream,
                            patch("bb_workflow.cli.Workflow") as workflow,
                            contextlib.redirect_stdout(io.StringIO()),
                            contextlib.redirect_stderr(io.StringIO()),
                        ):
                            stream.buffer.read.side_effect = AssertionError("stdin consumed")
                            argv = [command, *selection, *(["--json"] if machine else [])]
                            if machine:
                                self.assertEqual(main(argv), expected)
                            else:
                                with self.assertRaises(SystemExit) as raised:
                                    main(argv)
                                self.assertEqual(raised.exception.code, expected)
                            stream.buffer.read.assert_not_called()
                            workflow.assert_not_called()

    def test_bad_contents_and_read_failures_have_no_workflow_side_effects(self):
        for command in ("plan", "run"):
            for content in (b"", b" \r\n\t", b"\xff"):
                for source in ("file", "stdin"):
                    with (
                        self.subTest(command=command, content=content, source=source),
                        tempfile.TemporaryDirectory() as temp,
                    ):
                        repo = Path(temp)
                        path = repo / "task.txt"
                        path.write_bytes(content)
                        with (
                            patch(
                                "sys.stdin", io.TextIOWrapper(io.BytesIO(content), encoding="ascii")
                            ),
                            patch("bb_workflow.cli.Workflow") as workflow,
                        ):
                            code, result, _ = self.invoke(
                                [
                                    command,
                                    "--task-file",
                                    str(path) if source == "file" else "-",
                                    "--repo",
                                    temp,
                                    "--json",
                                ]
                            )
                        self.assertEqual(code, 2)
                        self.assertEqual(result["error"]["code"], "invalid_arguments")
                        workflow.assert_not_called()
                        self.assertFalse((repo / ".bb-workflow").exists())
            for failure, expected, code in (
                (OSError("unreadable"), "io_error", 2),
                (KeyboardInterrupt(), "interrupted", 130),
            ):
                for source in ("file", "stdin"):
                    with (
                        self.subTest(command=command, failure=failure, source=source),
                        tempfile.TemporaryDirectory() as temp,
                    ):
                        with (
                            patch("bb_workflow.cli.Workflow") as workflow,
                            patch("sys.stdin") as stream,
                            patch.object(Path, "read_bytes", side_effect=failure),
                        ):
                            stream.buffer.read.side_effect = failure
                            actual, result, _ = self.invoke(
                                [
                                    "--json",
                                    command,
                                    "--task-file",
                                    "task.txt" if source == "file" else "-",
                                    "--repo",
                                    temp,
                                ]
                            )
                        self.assertEqual(actual, code)
                        self.assertEqual(result["error"]["code"], expected)
                        workflow.assert_not_called()
                        self.assertFalse((Path(temp) / ".bb-workflow").exists())
            with tempfile.TemporaryDirectory() as temp:
                code, result, _ = self.invoke(
                    [command, "--task-file", str(Path(temp) / "missing"), "--json"]
                )
                self.assertEqual(code, 2)
                self.assertEqual(result["error"]["code"], "io_error")

    def test_legacy_input_errors_keep_stderr_and_exit_codes(self):
        for command in ("plan", "run"):
            for failure, code in (
                (None, 2),
                (OSError("read failed"), 2),
                (KeyboardInterrupt(), 130),
            ):
                with (
                    self.subTest(command=command, failure=failure),
                    tempfile.TemporaryDirectory() as temp,
                ):
                    output, errors = io.StringIO(), io.StringIO()
                    with (
                        patch("sys.stdin", io.StringIO(" ")) as stream,
                        patch("bb_workflow.cli.Workflow") as workflow,
                        contextlib.redirect_stdout(output),
                        contextlib.redirect_stderr(errors),
                    ):
                        if failure is not None:
                            stream.read = lambda: (_ for _ in ()).throw(failure)
                        self.assertEqual(main([command, "--task-file", "-", "--repo", temp]), code)
                    self.assertEqual(output.getvalue(), "")
                    self.assertTrue(errors.getvalue())
                    workflow.assert_not_called()
                    self.assertFalse((Path(temp) / ".bb-workflow").exists())

    def test_dash_file_json_placement_and_legacy_positionals(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.chdir(temp):
            Path("-").write_bytes(b"unsafe\r\n")
            for argv in (
                ["--json", "plan", "--task-file", "./-"],
                ["plan", "--task-file", "./-", "--json"],
            ):
                code, result, _ = self.invoke(argv)
                self.assertEqual(code, 0)
                self.assertEqual(result["data"]["complexity"], "high")
            code, result, _ = self.invoke(["plan", "--", "--json"])
            self.assertEqual(code, 0)
            self.assertNotIn("schema_version", result)
            code, _, _ = self.invoke(["plan", "", "--json"])
            self.assertEqual(code, 0)
            code, result, _ = self.invoke(["run", "", "--json"])
            self.assertEqual(code, 2)
            self.assertEqual(result["error"]["code"], "invalid_arguments")

    def test_redirected_subprocess_stdin_uses_utf8_bytes(self):
        task = "unsafe Ω\r\n'quoted' $(false) --json\n" * 9000
        environment = {
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "PYTHONIOENCODING": "ascii",
        }
        result = subprocess.run(
            [sys.executable, "-m", "bb_workflow", "--json", "plan", "--task-file", "-"],
            input=task.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["data"]["complexity"], "high")


class JsonContractTests(unittest.TestCase):
    """Command boundaries must stay parseable without changing legacy payloads."""

    def invoke(self, argv):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(argv)
        return code, output.getvalue(), errors.getvalue()

    def envelope(self, argv, command, code=0, error=None):
        actual, output, stderr = self.invoke(argv)
        self.assertEqual(actual, code, stderr)
        payload = json.loads(output)
        self.assertEqual(
            set(payload), {"schema_version", "command", "ok", "exit_code", "data", "error"}
        )
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["command"], command)
        self.assertEqual(payload["exit_code"], code)
        self.assertEqual(payload["ok"], code == 0)
        if error is None:
            self.assertIsNone(payload["error"])
        else:
            problem = payload["error"]
            self.assertEqual(
                set(problem), {"code", "message", "retryable", "next_action", "artifacts"}
            )
            self.assertEqual(problem["code"], error)
            self.assertFalse(problem["retryable"])
            self.assertTrue(problem["message"])
            self.assertTrue(problem["next_action"])
            self.assertTrue(all(Path(p).is_file() for p in problem["artifacts"]))
        return payload, stderr

    def test_plan_mode_placement_preserves_payload_and_requirements_needed_success(self):
        with tempfile.TemporaryDirectory() as temp:
            argv = ["plan", "task", "--repo", temp]
            code, output, _ = self.invoke(argv)
            self.assertEqual(code, 0)
            legacy = json.loads(output)
            for machine in (["--json", *argv], [*argv, "--json"]):
                payload, _ = self.envelope(machine, "plan")
                self.assertEqual(payload["data"], legacy)
                self.assertTrue(payload["data"]["requirements_needed"])

    def test_literal_json_task_does_not_enable_mode(self):
        code, output, _ = self.invoke(["plan", "--", "--json"])
        self.assertEqual(code, 0)
        self.assertNotIn("schema_version", json.loads(output))
        payload, _ = self.envelope(["--json", "plan", "--", "--json"], "plan")
        self.assertTrue(payload["data"]["requirements_needed"])

    def test_help_and_parse_errors_are_single_envelopes_with_actual_command(self):
        for argv, command in (
            (["--json", "--help"], None),
            (["plan", "--json", "--help"], "plan"),
        ):
            payload, stderr = self.envelope(argv, command)
            self.assertIn("usage:", payload["data"]["help"])
            self.assertEqual(stderr, "")
        for argv, command in (
            (["--json"], None),
            (["--json", "nonsense"], None),
            (["plan", "--json"], "plan"),
            (["plan", "task", "--json", "--bad"], "plan"),
            (["--json", "plan", "--config", "run"], "plan"),
        ):
            payload, stderr = self.envelope(argv, command, 2, "invalid_arguments")
            self.assertIsNone(payload["data"])
            self.assertEqual(stderr, "")
        for argv, expected in ((["--help"], 0), (["plan"], 2)):
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as raised:
                    main(argv)
            self.assertEqual(raised.exception.code, expected)

    def test_init_reports_path_and_existing_file_is_io_error(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "workflow.toml"
            payload, _ = self.envelope(["init", "--json", "--config", str(path)], "init")
            self.assertEqual(payload["data"], {"config": str(path.resolve())})
            self.assertTrue(path.is_file())
            original = path.read_bytes()
            self.envelope(["--json", "init", "--config", str(path)], "init", 2, "io_error")
            self.assertEqual(path.read_bytes(), original)

    def test_config_requirements_and_missing_state_have_distinct_codes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            missing = str(root / "missing.toml")
            self.envelope(
                ["plan", "task", "--json", "--config", missing], "plan", 2, "invalid_config"
            )
            self.envelope(
                ["plan", "task", "--json", "--requirements", missing],
                "plan",
                2,
                "invalid_requirements",
            )
            self.envelope(["status", temp, "--json"], "status", 2, "io_error")

    def test_invalid_requirement_binding_retains_category_inside_failed_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            requirements = root / "requirements.toml"
            requirements.write_text(
                "requirements_version = 1\n[[criteria]]\n"
                'id = "regression"\ndescription = "Verify the change"\n'
                'kind = "mechanical"\nverification = "Run the required check"\n'
                'check_id = "missing-check"\n'
            )
            for command, code in (("plan", 2), ("run", 1)):
                with self.subTest(command=command):
                    payload, _ = self.envelope(
                        [
                            command,
                            "task",
                            "--repo",
                            temp,
                            "--requirements",
                            str(requirements),
                            "--json",
                        ],
                        command,
                        code,
                        "invalid_requirements",
                    )
                    self.assertEqual(payload["error"]["next_action"], "correct_requirements")
                    if command == "run":
                        self.assertEqual(payload["data"]["status"], "failed")
                        state = json.loads(
                            (Path(payload["data"]["run_dir"]) / "state.json").read_text()
                        )
                        self.assertEqual(state["error_code"], "invalid_requirements")
                        self.assertEqual(state["agent_calls"], 0)
            self.assertFalse((root / ".bb-workflow/lock").exists())

    def test_verification_source_read_failures_preserve_typed_category(self):
        from bb_workflow import workspace

        for failure, error, action in (
            (PermissionError("fixture source is unreadable"), "io_error", "inspect_filesystem"),
            (
                workspace.WorkspaceError("fixture source cannot be captured"),
                "workspace_error",
                "inspect_workspace",
            ),
            (
                workspace.WorkspaceError("fixture source changed", code="source_conflict"),
                "source_conflict",
                "inspect_source_conflict",
            ),
        ):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source = root / "source.txt"
                source.write_text("source")
                original = workspace._file_hash

                def unreadable(path):
                    if path == source:
                        raise failure
                    return original(path)

                with patch("bb_workflow.workspace._file_hash", side_effect=unreadable):
                    payload, _ = self.envelope(
                        ["verify", "gate", "--repo", temp, "--json"], "verify", 1, error
                    )
                self.assertEqual(payload["data"]["status"], "failed")
                self.assertEqual(payload["error"]["next_action"], action)
                report = json.loads((Path(payload["data"]["run_dir"]) / "report.json").read_text())
                self.assertEqual(report["error_code"], error)
                self.assertEqual(report["results"], [])
                self.assertFalse(report["apply_eligible"])
                self.assertFalse((root / ".bb-workflow/lock").exists())

    def test_status_failed_read_is_success_and_preserves_legacy_payload(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = {"status": "failed", "reason": "provider went away", "unknown_cost_calls": 1}
            (root / "state.json").write_text(json.dumps(state))
            payload, _ = self.envelope(["status", temp, "--json"], "status")
            self.assertEqual(payload["data"], state)
            _, output, _ = self.invoke(["status", temp, "--summary"])
            summary, _ = self.envelope(["--json", "status", temp, "--summary"], "status")
            self.assertEqual(summary["data"], json.loads(output))

    def test_malformed_state_fails_without_traceback_in_both_modes(self):
        with tempfile.TemporaryDirectory() as temp:
            for content in ("{", "[]", "null", '{"acceptance": []}', '{"status": []}'):
                (Path(temp) / "state.json").write_text(content)
                for summary in ([], ["--summary"]):
                    self.envelope(
                        ["status", temp, "--json", *summary], "status", 2, "invalid_state"
                    )
                    code, output, errors = self.invoke(["status", temp, *summary])
                    self.assertEqual(code, 2)
                    self.assertEqual(output, "")
                    self.assertIn("Error:", errors)

    def test_nonfinite_nested_state_is_invalid_in_full_and_summary_modes(self):
        with tempfile.TemporaryDirectory() as temp:
            for number in ("NaN", "Infinity", "-Infinity", "1e400"):
                with self.subTest(number=number):
                    (Path(temp) / "state.json").write_text(
                        '{"status":"running","extra":{"values":[' + number + "]}}"
                    )
                    for summary in ([], ["--summary"]):
                        payload, _ = self.envelope(
                            ["status", temp, "--json", *summary], "status", 2, "invalid_state"
                        )
                        self.assertIsNone(payload["data"])

    def test_nonfinite_nested_progress_is_omitted_from_strict_json_status(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "state.json").write_text('{"status":"running"}')
            progress = {
                "version": 1,
                "run_dir": str(directory.resolve()),
                "pid": 123,
                "phase": "implementation",
                "active_operation": {"role": "implementation"},
                "updated_at": "2026-10-25T10:00:00+00:00",
                "elapsed_seconds": 5.0,
                "heartbeat_interval_seconds": 5.0,
            }
            for field in ("active_operation", "extra"):
                for number in ("NaN", "Infinity", "-Infinity", "1e400"):
                    with self.subTest(field=field, number=number):
                        content = json.dumps({**progress, field: {"values": ["NUMBER"]}})
                        (directory / "progress.json").write_text(
                            content.replace('"NUMBER"', number)
                        )
                        payload, _ = self.envelope(
                            ["status", temp, "--summary", "--json"], "status"
                        )
                        self.assertNotIn("progress", payload["data"])
                        json.dumps(payload, allow_nan=False)

    def test_machine_serialization_falls_back_to_strict_error_for_nonfinite_payload(self):
        from bb_workflow.cli_output import CommandResult

        with patch(
            "bb_workflow.cli._dispatch", return_value=CommandResult({"extra": [float("nan")]})
        ):
            payload, _ = self.envelope(
                ["status", "unused", "--json"], "status", 2, "internal_error"
            )
        self.assertIsNone(payload["data"])

    def test_artifact_probe_failures_preserve_original_error_and_other_artifacts(self):
        from bb_workflow.cli import _InvalidState

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("state.json", "report.md", "report.json"):
                (root / name).write_text("evidence")
            is_file, resolve = Path.is_file, Path.resolve

            def denied_probe(path):
                if path == root / "state.json":
                    raise PermissionError("probe denied")
                return is_file(path)

            def denied_resolve(path, *args, **kwargs):
                if path == root / "report.md":
                    raise OSError("resolve denied")
                return resolve(path, *args, **kwargs)

            with (
                patch("bb_workflow.cli._read_state", side_effect=_InvalidState("original problem")),
                patch.object(Path, "is_file", denied_probe),
                patch.object(Path, "resolve", denied_resolve),
            ):
                payload, _ = self.envelope(["status", temp, "--json"], "status", 2, "invalid_state")
            self.assertEqual(payload["error"]["message"], "original problem")
            self.assertEqual(payload["error"]["artifacts"], [str(root / "report.json")])

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "POSIX file permissions")
    def test_unreadable_run_directory_returns_original_io_error_envelope(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "inaccessible"
            directory.mkdir()
            directory.chmod(0)
            try:
                payload, _ = self.envelope(
                    ["status", str(directory), "--json"], "status", 2, "io_error"
                )
                self.assertIn("state.json", payload["error"]["message"])
                self.assertEqual(payload["error"]["artifacts"], [])
            finally:
                directory.chmod(0o700)

    @unittest.skipUnless(os.name == "posix", "POSIX pipes")
    def test_real_closed_stderr_pipe_keeps_cli_stdout_and_exit_code(self):
        for flags in ([], ["--events-jsonl"], ["--json", "--events-jsonl"]):
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as temp:
                reader, writer = os.pipe()
                os.close(reader)
                try:
                    result = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "bb_workflow.cli",
                            "run",
                            "task",
                            "--repo",
                            temp,
                            *flags,
                        ],
                        stdout=subprocess.PIPE,
                        stderr=writer,
                        text=True,
                        timeout=10,
                    )
                finally:
                    os.close(writer)
                self.assertEqual(result.returncode, 1)
                payload = json.loads(result.stdout)
                data = payload["data"] if "--json" in flags else payload
                self.assertEqual(data["status"], "blocked")
                directory = Path(data["run_dir"])
                self.assertEqual(
                    json.loads((directory / "state.json").read_text())["status"], "blocked"
                )
                self.assertFalse((Path(temp) / ".bb-workflow/lock").exists())

    def test_closed_stderr_keeps_run_stdout_and_terminal_evidence_usable(self):
        class ClosedConsumer(io.StringIO):
            def write(self, text):
                if self.getvalue():
                    raise BrokenPipeError("consumer closed after start")
                return super().write(text)

        for flags in ([], ["--json"], ["--events-jsonl"], ["--json", "--events-jsonl"]):
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as temp:
                output = io.StringIO()
                with (
                    contextlib.redirect_stdout(output),
                    contextlib.redirect_stderr(ClosedConsumer()),
                ):
                    code = main(["run", "task", "--repo", temp, *flags])
                self.assertEqual(code, 1)
                payload = json.loads(output.getvalue())
                data = payload["data"] if "--json" in flags else payload
                self.assertEqual(data["status"], "blocked")
                directory = Path(data["run_dir"])
                self.assertEqual(
                    json.loads((directory / "state.json").read_text())["status"], "blocked"
                )
                self.assertTrue((directory / "report.md").is_file())
                self.assertFalse((Path(temp) / ".bb-workflow/lock").exists())

    def test_closed_stderr_does_not_rethrow_while_reporting_original_io_failure(self):
        class ClosedConsumer(io.StringIO):
            def write(self, text):
                raise BrokenPipeError("closed diagnostic stream")

        with tempfile.TemporaryDirectory() as temp:
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(ClosedConsumer()):
                code = main(["status", temp])
            self.assertEqual(code, 2)
            self.assertEqual(output.getvalue(), "")

    def test_missing_run_requirements_retains_blocked_exit_and_progress_stderr(self):
        with tempfile.TemporaryDirectory() as temp:
            payload, stderr = self.envelope(
                ["run", "task", "--repo", temp, "--json", "--events-jsonl"],
                "run",
                1,
                "requirements_required",
            )
            self.assertEqual(payload["data"]["status"], "blocked")
            directory = Path(payload["data"]["run_dir"])
            state = json.loads((directory / "state.json").read_text())
            self.assertEqual(state["error_code"], "requirements_required")
            events = [json.loads(line) for line in stderr.splitlines()]
            self.assertEqual([events[0]["event"], events[-1]["event"]], ["start", "finish"])
            self.assertIn(str(directory / "state.json"), payload["error"]["artifacts"])

    def test_project_lock_code_does_not_delete_lock(self):
        from bb_workflow.workspace import project_lock

        with tempfile.TemporaryDirectory() as temp:
            with project_lock(Path(temp)):
                self.envelope(["run", "task", "--repo", temp, "--json"], "run", 2, "project_locked")
                self.assertTrue((Path(temp) / ".bb-workflow/lock").is_file())

    def test_verify_envelope_preserves_payload_and_failure_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "workflow.toml"
            for command, code, error in (("pass", 0, None), ("raise SystemExit(1)", 1, "failed")):
                path.write_text(
                    default_text()
                    + "\n[verification.profiles.fast]\nchecks = "
                    + json.dumps([[sys.executable, "-c", command]])
                )
                payload, _ = self.envelope(
                    ["verify", "fast", "--repo", temp, "--json"], "verify", code, error
                )
                self.assertFalse(payload["data"]["apply_eligible"])
                self.assertEqual(
                    set(payload["data"]),
                    {"status", "reason", "run_dir", "report", "apply_eligible"},
                )

    def test_doctor_payload_and_metadata_failure(self):
        for supported, code, error in ((True, 0, None), (False, 1, "doctor_failed")):
            with (
                patch("bb_workflow.cli.probe", return_value={"supported": supported}),
                patch("bb_workflow.cli.run_process", return_value=ProcessResult(0, "v1", "", 0)),
            ):
                _, output, _ = self.invoke(["doctor"])
                payload, _ = self.envelope(["--json", "doctor"], "doctor", code, error)
            self.assertEqual(payload["data"], json.loads(output))

    def test_real_run_results_keep_payload_and_preserve_failure_evidence(self):
        from bb_workflow.backends import BackendError, BackendResult
        from bb_workflow.engine import Workflow

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = default_text().replace("config_version = 3", "config_version = 2")
            start = config.index("checks = ")
            end = config.index("\nexclude = ", start)
            config = (
                config[:start]
                + "checks = "
                + json.dumps([[sys.executable, "-c", "pass"]])
                + config[end:]
            )
            start_prepare = config.index("prepare = ")
            end_prepare = config.index("\n", start_prepare)
            config = config[:start_prepare] + "prepare = []" + config[end_prepare:]
            (root / "workflow.toml").write_text(config)
            (root / "source.txt").write_text("original")
            for failure, code, error in (
                (None, 0, None),
                (BackendError("different provider message", retryable=True), 1, "provider_error"),
                (KeyboardInterrupt(), 130, "interrupted"),
            ):

                def fixture(**kwargs):
                    if failure is not None:
                        raise failure
                    if kwargs["role"] == "implementation":
                        (kwargs["cwd"] / "source.txt").write_text("candidate")
                    return BackendResult(
                        {
                            "status": "completed",
                            "summary": "fixture",
                            "findings": [],
                            "decisions": [],
                        },
                        None,
                        {},
                        kwargs["selection"].model,
                    )

                def make(*args, **kwargs):
                    return Workflow(*args, invoke_fn=fixture, **kwargs)

                with patch("bb_workflow.cli.Workflow", side_effect=make):
                    payload, stderr = self.envelope(
                        ["run", "task", "--repo", temp, "--json"], "run", code, error
                    )
                self.assertEqual(set(payload["data"]), {"status", "reason", "run_dir", "report"})
                self.assertIn("Run directory:", stderr)
                state = json.loads((Path(payload["data"]["run_dir"]) / "state.json").read_text())
                self.assertGreater(state["unknown_cost_calls"], 0)
                self.assertEqual(
                    state["status"],
                    "succeeded" if failure is None else "interrupted" if code == 130 else "failed",
                )

    def test_failed_report_is_not_linked_when_it_does_not_exist(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch(
                "bb_workflow.engine.Workflow._report", side_effect=OSError("report write failed")
            ):
                payload, _ = self.envelope(
                    ["run", "task", "--repo", temp, "--json"], "run", 1, "io_error"
                )
            self.assertEqual(
                payload["error"]["artifacts"],
                [str(Path(payload["data"]["run_dir"]) / "state.json")],
            )

    def test_verify_source_mutation_has_semantic_conflict_code(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            command = [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('source.txt').write_text('changed')",
            ]
            (root / "workflow.toml").write_text(
                default_text() + "\n[verification.profiles.fast]\nchecks = " + json.dumps([command])
            )
            payload, _ = self.envelope(
                ["verify", "fast", "--repo", temp, "--json"], "verify", 1, "source_conflict"
            )
            self.assertFalse(
                json.loads((Path(payload["data"]["run_dir"]) / "report.json").read_text())[
                    "source_unchanged"
                ]
            )

    def test_interrupt_after_dispatch_still_returns_machine_result(self):
        from bb_workflow.cli_output import emit_result

        calls = 0

        def interrupted_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise KeyboardInterrupt()
            return emit_result(*args, **kwargs)

        with patch("bb_workflow.cli.emit_result", side_effect=interrupted_once):
            self.envelope(["--json", "plan", "task"], "plan", 130, "interrupted")

    def test_outer_interrupt_and_unclassified_exception_are_machine_errors(self):
        for exception, code, error in (
            (KeyboardInterrupt(), 130, "interrupted"),
            (RuntimeError("unclassified failure"), 2, "internal_error"),
            (ValueError("unclassified value"), 2, "internal_error"),
        ):
            with patch("bb_workflow.cli._config", side_effect=exception):
                self.envelope(["--json", "run", "task"], "run", code, error)
