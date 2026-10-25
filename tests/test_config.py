import tempfile
import unittest
from pathlib import Path

from bb_workflow.config import (
    CURRENT_CONFIG_VERSION,
    DEFAULT_CHECKS,
    LEGACY_CHECKS,
    Config,
    ConfigError,
    ContextConfig,
    default_text,
    load_config,
    route,
)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "workflow.toml"

    def write(self, text):
        self.path.write_text(text, encoding="utf-8")
        return load_config(self.path)

    def test_strict_verification_config(self):
        invalid = [
            "[verification]\nunknown = true",
            '[verification.profiles.nope]\nvariants = ["default"]',
            '[verification.profiles.gate]\nchecks = [["cargo", "check"]]',
            "[verification.profiles.fast]\nchecks = []",
            "[verification.profiles.fast]\nvariants = []",
            '[verification.profiles.fast]\nvariants = ["missing"]',
            '[verification.profiles.fast]\nvariants = ["default", "default"]',
            '[verification.variants.x]\nfeatures = [""]',
            '[verification.variants.x]\nfeatures = ["a"]\nall_features = true',
            '[verification.variants.x]\nall_features = "true"',
            "[verification.variants.x]\nno_default_features = 1",
            '[verification.variants.x]\ntarget = " "',
            '[verification.variants.x]\ntoolchain = ""',
            "[verification.variants.x]\nunknown = true",
            "verification = []",
        ]
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(ConfigError):
                self.write(default_text() + "\n" + text)

    def test_default_template_loads_all_profiles(self):
        config = load_config()
        self.assertEqual(config.max_cycles, 3)
        self.assertEqual(config.max_agent_calls, 20)
        self.assertEqual(config.max_output_bytes, 16 * 1024 * 1024)
        self.assertEqual(config.config_version, 2)
        self.assertEqual(config.prepare, [["cargo", "check", "--workspace", "--all-targets"]])
        self.assertEqual(set(config.profiles), {"low", "medium", "high"})
        self.assertEqual(config.profiles["low"]["implementation"].model, "claude-haiku-5-5")
        self.assertEqual(config.profiles["low"]["implementation"].effort, "low")
        self.assertEqual(config.profiles["medium"]["implementation"].model, "claude-sonnet-5-5")
        self.assertEqual(config.profiles["high"]["implementation"].model, "claude-opus-5-5")

    def test_new_defaults_cover_workspace_and_doctests_in_order(self):
        expected = [
            ["cargo", "fmt", "--all", "--", "--check"],
            ["cargo", "check", "--workspace", "--all-targets", "--locked"],
            ["cargo", "test", "--workspace", "--all-targets", "--locked"],
            ["cargo", "test", "--workspace", "--doc", "--locked"],
            [
                "cargo",
                "clippy",
                "--workspace",
                "--all-targets",
                "--locked",
                "--",
                "-D",
                "warnings",
            ],
        ]
        self.assertEqual(CURRENT_CONFIG_VERSION, 2)
        self.assertEqual(Config().config_version, 2)
        self.assertEqual(Config().checks, expected)
        self.assertEqual(load_config().checks, expected)
        self.assertEqual(DEFAULT_CHECKS, tuple(tuple(command) for command in expected))
        without_checks = "\n".join(
            line for line in default_text().splitlines() if not line.startswith("checks =")
        )
        self.assertEqual(self.write(without_checks).checks, expected)

    def test_unversioned_and_v1_missing_checks_keep_the_original_fallback(self):
        expected = [
            ["cargo", "fmt", "--check"],
            ["cargo", "check", "--all-targets"],
            ["cargo", "test", "--all-targets"],
            ["cargo", "clippy", "--all-targets", "--", "-D", "warnings"],
        ]
        self.assertEqual(LEGACY_CHECKS, tuple(tuple(command) for command in expected))
        for version_line in ("", "config_version = 1"):
            text = "\n".join(
                version_line if line.startswith("config_version =") else line
                for line in default_text().splitlines()
                if not line.startswith("checks =")
            )
            with self.subTest(version_line=version_line):
                config = self.write(text)
                self.assertEqual(config.config_version, 1)
                self.assertEqual(config.checks, expected)

    def test_explicit_checks_are_preserved_for_all_supported_versions(self):
        historical_template = (
            '[ ["cargo", "fmt", "--check"], '
            '["cargo", "check", "--all-targets", "--locked"], '
            '["cargo", "test", "--all-targets", "--locked"], '
            '["cargo", "clippy", "--all-targets", "--locked", "--", "-D", "warnings"] ]'
        )
        custom = '[["custom runner", "argument with spaces", "--test", "a"], ["true"]]'
        for version_line in ("", "config_version = 1", "config_version = 2"):
            for checks in (historical_template, custom):
                text = "\n".join(
                    version_line
                    if line.startswith("config_version =")
                    else "checks = " + checks
                    if line.startswith("checks =")
                    else line
                    for line in default_text().splitlines()
                )
                with self.subTest(version_line=version_line, checks=checks):
                    config = self.write(text)
                    if checks == custom:
                        self.assertEqual(
                            config.checks,
                            [["custom runner", "argument with spaces", "--test", "a"], ["true"]],
                        )
                    else:
                        self.assertEqual(
                            config.checks,
                            [
                                ["cargo", "fmt", "--check"],
                                ["cargo", "check", "--all-targets", "--locked"],
                                ["cargo", "test", "--all-targets", "--locked"],
                                [
                                    "cargo",
                                    "clippy",
                                    "--all-targets",
                                    "--locked",
                                    "--",
                                    "-D",
                                    "warnings",
                                ],
                            ],
                        )

    def test_config_version_rejects_unsupported_versions_and_invalid_types(self):
        for value in ("0", "-1", "3", "99", "true", "false", "2.0", '"2"', "[]", "{}"):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "config_version"):
                self.write(
                    default_text().replace("config_version = 2", "config_version = " + value)
                )

    def test_default_check_commands_are_isolated_between_configurations(self):
        first = Config()
        second = Config()
        first.checks[0].append("changed")
        first.checks.append(["extra"])
        self.assertEqual(second.checks, [list(command) for command in DEFAULT_CHECKS])
        self.assertEqual(Config().checks, second.checks)
        for version in (1, 2):
            text = "\n".join(
                line.replace("config_version = 2", f"config_version = {version}")
                for line in default_text().splitlines()
                if not line.startswith("checks =")
            )
            with self.subTest(version=version):
                loaded = self.write(text)
                loaded.checks[0].append("changed")
                expected = LEGACY_CHECKS if version == 1 else DEFAULT_CHECKS
                self.assertEqual(self.write(text).checks, [list(command) for command in expected])

    def test_auto_routing_covers_english_polish_and_default(self):
        config = load_config()
        level, selections = route(config, "Review unsafe concurrent Rust change")
        self.assertEqual(level, "high")
        self.assertEqual(selections["triage"].effort, "high")
        self.assertEqual(route(config, "Popraw literówkę w README")[0], "low")
        self.assertEqual(route(config, "Implement new parser and update README")[0], "medium")
        self.assertEqual(route(config, "Improve security and update README")[0], "high")
        self.assertEqual(route(config, "Add a new endpoint")[0], "medium")

    def test_context_defaults_also_apply_to_existing_configs_without_context(self):
        text = default_text()
        start = text.index("[context]\n")
        end = text.index("\n[", start + 1)
        legacy = text[:start] + text[end + 1 :]
        expected = ContextConfig(16000, 2000, "reference")
        self.assertEqual(load_config().context, expected)
        self.assertEqual(self.write(legacy).context, expected)

    def test_context_accepts_zero_excerpt_limits_and_inline_skills(self):
        text = (
            default_text()
            .replace("max_diff_chars = 16000", "max_diff_chars = 0")
            .replace("failed_check_tail_chars = 2000", "failed_check_tail_chars = 0")
            .replace('skill_mode = "reference"', 'skill_mode = "inline"')
        )
        self.assertEqual(self.write(text).context, ContextConfig(0, 0, "inline"))

    def test_context_rejects_invalid_types_negative_limits_and_unknown_options(self):
        for key, default in (("max_diff_chars", 16000), ("failed_check_tail_chars", 2000)):
            for value in ("true", "-1", "1.5", '"2000"', "[]"):
                with self.subTest(key=key, value=value), self.assertRaises(ConfigError):
                    self.write(default_text().replace(f"{key} = {default}", f"{key} = {value}"))
        for value in ('"automatic"', "true", "0", "[]", "{}"):
            with self.subTest(skill_mode=value), self.assertRaises(ConfigError):
                self.write(
                    default_text().replace('skill_mode = "reference"', f"skill_mode = {value}")
                )
        with self.assertRaises(ConfigError):
            self.write(default_text().replace("[context]", "[context]\nunknown = 1"))
        text = default_text()
        start = text.index("[context]\n")
        end = text.index("\n[", start + 1)
        legacy = text[:start] + text[end + 1 :]
        with self.assertRaisesRegex(ConfigError, "context must be a table"):
            self.write("context = false\n" + legacy)

    def test_prepare_commands_are_optional_and_validated(self):
        original = 'prepare = [["cargo", "check", "--workspace", "--all-targets"]]'
        text = default_text()
        self.assertEqual(self.write(text.replace(original, "prepare = []")).prepare, [])
        self.assertEqual(self.write(text.replace(original, "")).prepare, [])
        for value in ("true", '"cargo check"', "[[]]", "[[false]]", '[[""]]'):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                self.write(text.replace(original, "prepare = " + value))

    def test_output_limit_requires_positive_integer(self):
        original = "max_output_bytes = 16777216"
        self.assertEqual(
            self.write(
                default_text().replace(original, "max_output_bytes = 1024")
            ).max_output_bytes,
            1024,
        )
        for value in ("0", "-1", "true", "1.5", '"1024"'):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                self.write(default_text().replace(original, "max_output_bytes = " + value))

    def test_explicit_complexity_and_role_override(self):
        override = '[roles.reviewer_b]\nbackend="codex"\nmodel="gpt-6.1-sol"\neffort="ultra"\n'
        config = self.write(default_text() + "\n" + override)
        level, selections = route(config, "rename a function", "medium")
        self.assertEqual(level, "medium")
        self.assertEqual(selections["reviewer_b"].effort, "ultra")

    def test_invalid_limits_unknown_fields_and_efforts_fail(self):
        for fragment in (
            "max_cycles = true",
            "max_cycles = -1",
            "max_agent_calls = 0",
            "timeout_seconds = nan",
            'surprise = "no"',
            'effort = "ultra"',
            'blocking_severities = ["info"]',
            "checks = []",
        ):
            with self.subTest(fragment=fragment):
                text = default_text()
                if fragment.startswith("max_cycles") or fragment.startswith("timeout_seconds"):
                    text = (
                        text.replace("max_cycles = 3", fragment)
                        if fragment.startswith("max_cycles")
                        else text.replace("timeout_seconds = 900", fragment)
                    )
                elif fragment.startswith("surprise"):
                    text = text.replace("exclude = []", "exclude = []\n" + fragment)
                else:
                    text = text.replace('effort = "medium"', fragment, 1)
                with self.assertRaises(ConfigError):
                    self.write(text)

    def test_model_catalog_extension_and_tool_allowlist(self):
        text = default_text().replace(
            "[profiles.low.implementation]", '[profiles.low.implementation]\ntools = ["shell"]'
        )
        with self.assertRaises(ConfigError):
            self.write(text)
        extended = default_text().replace(
            "[executables]",
            '[model_catalog.codex]\ncustom = ["standard"]\n\n[roles.reviewer_b]\nbackend="codex"\nmodel="custom"\neffort="standard"\n\n[executables]',
        )
        config = self.write(extended)
        self.assertEqual(config.roles["reviewer_b"].model, "custom")

    def test_codex_rejects_mcp_and_lsp_configuration(self):
        for extra in ('mcp_config = "mcp.json"', 'tools = ["LSP"]'):
            text = default_text().replace(
                "[profiles.low.reviewer_b]", "[profiles.low.reviewer_b]\n" + extra
            )
            with self.subTest(extra=extra), self.assertRaises(ConfigError):
                self.write(text)

    def test_claude_catalog_accepts_pinned_legacy_efforts_only(self):
        config = self.write(
            default_text()
            + '\n[roles.implementation]\nbackend="claude"\nmodel="claude-opus-4-6"\neffort="max"\n'
        )
        self.assertEqual(config.roles["implementation"].effort, "max")
        text = (
            default_text()
            + '\n[roles.implementation]\nbackend="claude"\nmodel="claude-opus-4-6"\neffort="xhigh"\n'
        )
        with self.assertRaises(ConfigError):
            self.write(text)

    def test_exact_mcp_tool_requires_server_config_and_rejects_wildcards(self):
        role = (
            '\n[roles.reviewer_a]\nbackend="claude"\nmodel="claude-sonnet-5-5"\neffort="medium"\n'
        )
        valid = role + 'tools=["mcp__rust__hover"]\nmcp_config="rust-mcp.json"\n'
        self.assertEqual(
            self.write(default_text() + valid).roles["reviewer_a"].tools, ("mcp__rust__hover",)
        )
        for bad in (
            role + 'tools=["mcp__rust__hover"]\n',
            role + 'tools=["mcp__rust__*"]\nmcp_config="rust-mcp.json"\n',
        ):
            with self.assertRaises(ConfigError):
                self.write(default_text() + bad)
        text = (
            default_text()
            + '\n[roles.implementation]\nbackend="claude"\nmodel="claude-haiku-4-5"\n'
        )
        with self.assertRaises(ConfigError):
            self.write(text)


if __name__ == "__main__":
    unittest.main()
