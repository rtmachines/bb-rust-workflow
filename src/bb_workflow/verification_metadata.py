"""Conservative, audited tool identity for a single verification phase."""

from __future__ import annotations

import hashlib
import json
import re
import time
import tomllib
from pathlib import Path
from typing import Callable

from .config import Config
from .process import ProcessResult
from .workspace import WorkspaceError, manifest, write_json


class MetadataError(RuntimeError):
    """Metadata changed source and invalidated verification."""


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _tools(argv: tuple[str, ...], env: dict[str, str]) -> list[tuple[str, list[str]]]:
    if not argv:
        return []
    name = Path(argv[0]).name
    selector = [argv[1]] if len(argv) > 1 and argv[1].startswith("+") else []
    if name == "rustc":
        return [("rustc", [argv[0], *selector, "--version"])]
    offset = 1 + len(selector)
    if (
        name != "cargo"
        or len(argv) <= offset
        or argv[offset] not in {"check", "build", "test", "clippy", "fmt"}
    ):
        return []
    tools = [
        ("cargo", [argv[0], *selector, "--version"]),
        ("rustc", [env.get("RUSTC") or "rustc", *selector, "--version"]),
    ]
    command = argv[offset]
    if command in {"fmt", "clippy"}:
        tools.append(
            (
                "rustfmt" if command == "fmt" else "clippy",
                [argv[0], *selector, command, "--version"],
            )
        )
    if command == "test" and "--doc" in argv:
        tools.append(("rustdoc", [env.get("RUSTDOC") or "rustdoc", *selector, "--version"]))
    return tools


def configuration(cwd: Path, config: Config, env: dict[str, str]) -> dict:
    """Hash relevant contents and environment values; never serialize secret values."""
    names = {
        "Cargo.toml",
        "Cargo.lock",
        "rust-toolchain",
        "rust-toolchain.toml",
        "clippy.toml",
        ".clippy.toml",
    }
    paths = {
        cwd / name: name
        for name in manifest(cwd, config.exclude)
        if Path(name).name in names or name.endswith((".cargo/config", ".cargo/config.toml"))
    }
    for level, parent in enumerate((cwd, *cwd.parents)):
        if level == 0 and config.exclude:
            continue
        for name in (
            ".cargo/config",
            ".cargo/config.toml",
            "rust-toolchain",
            "rust-toolchain.toml",
            "clippy.toml",
            ".clippy.toml",
        ):
            path = parent / name
            if path.is_file():
                paths.setdefault(path, f"ancestor/{level}/{name}")
    cargo_home = Path(env.get("CARGO_HOME") or (Path(env.get("HOME") or Path.home()) / ".cargo"))
    if not cargo_home.is_absolute():
        cargo_home = cwd / cargo_home
    for name in ("config", "config.toml"):
        path = cargo_home / name
        if path.is_file():
            paths.setdefault(path, f"cargo_home/{name}")
    if env.get("CLIPPY_CONF_DIR"):
        clippy_directory = Path(env["CLIPPY_CONF_DIR"])
        if not clippy_directory.is_absolute():
            clippy_directory = cwd / clippy_directory
        for name in ("clippy.toml", ".clippy.toml"):
            path = clippy_directory / name
            if path.is_file():
                paths.setdefault(path, f"clippy_conf_dir/{name}")
    files, reasons = {}, []
    if config.exclude:
        # Excluded directories may contain effective manifests/configuration. Do not
        # walk them or weaken the caller's existing source-exclusion contract.
        reasons.append("configuration_limited_by_custom_exclusions")
    for path, label in sorted(paths.items(), key=lambda pair: pair[1]):
        try:
            data = path.read_bytes()
            files[label] = hashlib.sha256(data).hexdigest()
            if (
                path.name in {"config", "config.toml"}
                and path.parent.name == ".cargo"
                or label.startswith("cargo_home/")
            ):
                parsed = tomllib.loads(data.decode())
                build = parsed.get("build", {})
                if not isinstance(build, dict):
                    raise ValueError("invalid Cargo build configuration")
                if any(
                    k in build
                    for k in ("rustc", "rustc-wrapper", "rustc-workspace-wrapper", "rustdoc")
                ):
                    reasons.append("configured_tool_override")
                # Included config may resolve outside the captured hierarchy.
                if "include" in parsed:
                    reasons.append("included_cargo_configuration")
        except (OSError, ValueError, UnicodeError) as exc:
            files[label] = None
            reasons.append(f"configuration_unreadable:{label}:{type(exc).__name__}")
    root_pattern = re.compile(r"(?<![\w./-])" + re.escape(str(cwd)) + r"(?=/|[\s\"'`:;,=]|$)")
    relevant = {
        key: _digest(root_pattern.sub(".", value))
        for key, value in env.items()
        if key.startswith(("CARGO_", "RUST", "CLIPPY_", "RUSTUP_"))
        or key
        in {
            "PATH",
            "HOME",
            "CC",
            "CXX",
            "AR",
            "LD",
            "PKG_CONFIG_PATH",
            "LIBRARY_PATH",
            "LD_LIBRARY_PATH",
            "DYLD_LIBRARY_PATH",
            "SDKROOT",
            "MACOSX_DEPLOYMENT_TARGET",
        }
    }
    if any(
        env.get(key)
        for key in (
            "RUSTC_WRAPPER",
            "RUSTC_WORKSPACE_WRAPPER",
            "CARGO_BUILD_RUSTC",
            "CARGO_BUILD_RUSTC_WRAPPER",
            "CARGO_BUILD_RUSTC_WORKSPACE_WRAPPER",
            "CARGO_BUILD_RUSTDOC",
        )
    ):
        reasons.append("environment_tool_override")
    return {
        "files": files,
        "environment": relevant,
        "reasons": sorted(set(reasons)),
        "digest": None
        if reasons or any(v is None for v in files.values())
        else _digest([files, relevant]),
    }


class MetadataCapture:
    """Cache probes only within this phase, with source guards around every launch."""

    def __init__(
        self,
        *,
        cwd: Path,
        artifact_root: Path,
        command_root: Path,
        config: Config,
        timeout: Callable[[], float],
        process_fn: Callable,
        frozen: dict,
    ):
        self.cwd, self.artifact_root, self.command_root = cwd, artifact_root, command_root
        self.config, self.timeout, self.process = config, timeout, process_fn
        self.frozen = frozen
        self.cache: dict[str, dict] = {}

    def capture(self, argv: tuple[str, ...], env: dict[str, str], directory: Path) -> dict:
        tools = _tools(argv, env)
        if not tools:
            metadata = {
                "files": {},
                "environment": {},
                "reasons": ["unrecognized_command"],
                "digest": None,
                "tool_versions": {},
                "probes": [],
            }
            write_json(directory / "metadata.json", metadata)
            return metadata
        metadata = configuration(self.cwd, self.config, env)
        metadata.update(tool_versions={}, probes=[])
        if any(arg == "--config" or arg.startswith("--config=") for arg in argv):
            metadata["reasons"].append("unresolved_cli_configuration_override")
        if len(argv) > 1 and argv[1].startswith("+") and env.get("RUSTC"):
            metadata["reasons"].append("explicit_compiler_toolchain_override")
        if any(
            "override" in reason or "included_cargo" in reason for reason in metadata["reasons"]
        ):
            # Do not guess that the default compiler is the compiler Cargo will launch.
            metadata["tool_versions"]["rustc"] = None
            tools = [(tool, command) for tool, command in tools if tool not in {"rustc", "rustdoc"}]
        if not tools:
            metadata["reasons"].append("unrecognized_command")
        try:
            for tool, command in tools:
                key = _digest([command, metadata["digest"], metadata["environment"]])
                if key not in self.cache:
                    probe_dir = self.command_root / "metadata" / str(len(self.cache))
                    probe_dir.mkdir(parents=True)
                    write_json(probe_dir / "command.json", command)
                    started = time.monotonic()
                    error = None
                    result = ProcessResult(-1, "", "", 0)
                    try:
                        result = self.process(
                            command,
                            cwd=self.cwd,
                            timeout=self.timeout(),
                            env=env,
                            max_output_bytes=self.config.max_output_bytes,
                        )
                    except BaseException as exc:
                        error = exc
                    (probe_dir / "stdout.txt").write_text(result.stdout, encoding="utf-8")
                    (probe_dir / "stderr.txt").write_text(result.stderr, encoding="utf-8")
                    evidence = {
                        "command": command,
                        "returncode": result.returncode,
                        "timed_out": result.timed_out,
                        "output_limit_exceeded": result.output_limit_exceeded,
                        "duration": time.monotonic() - started,
                        "artifacts": str(probe_dir.relative_to(self.artifact_root)),
                        "error": type(error).__name__ if error else None,
                    }
                    write_json(probe_dir / "result.json", evidence)
                    text = result.stdout.strip() or result.stderr.strip()
                    version = (
                        text
                        if (
                            not error
                            and result.returncode == 0
                            and not result.timed_out
                            and not result.output_limit_exceeded
                            and re.fullmatch(re.escape(tool) + r" \d+\.\d+[^\n]*", text)
                        )
                        else None
                    )
                    evidence["version"] = version
                    self.cache[key] = evidence
                    metadata["probes"].append(evidence)
                    try:
                        unchanged = manifest(self.cwd, self.config.exclude) == self.frozen
                    except (OSError, WorkspaceError):
                        unchanged = False
                    if not unchanged:
                        if isinstance(error, KeyboardInterrupt):
                            raise error
                        raise MetadataError(
                            "Metadata probe changed source files; results invalidated"
                        )
                    if error is not None and not isinstance(error, OSError):
                        raise error
                else:
                    metadata["probes"].append(self.cache[key])
                metadata["tool_versions"][tool] = self.cache[key]["version"]
                if self.cache[key]["version"] is None:
                    metadata["reasons"].append("unknown_tool_version:" + tool)
            if metadata["reasons"] and tools:
                # Relevant tool override semantics cannot be established by default probes.
                if any("override" in r or "included_cargo" in r for r in metadata["reasons"]):
                    metadata["tool_versions"]["rustc"] = None
            self.recheck(metadata, env, directory)
        finally:
            write_json(directory / "metadata.json", metadata)
        return metadata

    def recheck(self, metadata: dict, env: dict[str, str], directory: Path) -> None:
        """Configuration drift outside the source manifest leaves identity unknown."""
        if "unrecognized_command" in metadata["reasons"]:
            return
        try:
            current = configuration(self.cwd, self.config, env)
            drifted = current["digest"] != metadata["digest"]
        except (OSError, WorkspaceError):
            drifted = True
        if drifted:
            metadata["digest"] = None
            metadata["reasons"].append("configuration_changed_during_verification")
        write_json(directory / "metadata.json", metadata)
