"""Tests for build-godot.py CLI — argument parsing, exit codes, and guards."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

_SCRIPT = str(Path(__file__).parent.parent / "build-godot.py")


def _import_build_godot():
    """Load build-godot.py as a module so we can call cmd_containers directly.

    The script does not live on a package path (and has a hyphen in its name),
    so we import it via importlib's spec_from_file_location. We cache it on the
    function object to avoid re-loading per test.
    """
    cached = getattr(_import_build_godot, "_cached", None)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("build_godot_cli", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _import_build_godot._cached = module
    return module


_MINIMAL_VALID_TOML = """\
registry = "ghcr.io"
username = "testuser"
godot_version = "4.3"

[[platforms]]
name = "linux"
image = "ghcr.io/test/linux:4.3"
scons_flags = "platform=linuxbsd"
"""


def _run_cli(*args: str, **kwargs) -> subprocess.CompletedProcess:
    """Run build-godot.py via the current interpreter and return the result."""
    return subprocess.run(
        [sys.executable, _SCRIPT, *args],
        capture_output=True,
        text=True,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# --help
# ---------------------------------------------------------------------------


class TestCliHelp:
    def test_exits_zero_when_help_requested(self):
        result = _run_cli("--help")

        assert result.returncode == 0

    def test_help_output_mentions_build_subcommand(self):
        result = _run_cli("--help")

        assert "build" in result.stdout


# ---------------------------------------------------------------------------
# build — unsupported platform
# ---------------------------------------------------------------------------


class TestCliBuildUnsupportedPlatform:
    def test_exits_two_when_platform_is_not_supported(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(_MINIMAL_VALID_TOML, encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                _SCRIPT,
                "build",
                "--platform",
                "unsupported_xyz",
                "--config",
                str(config_file),
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 2


# ---------------------------------------------------------------------------
# build — missing config file
# ---------------------------------------------------------------------------


class TestCliBuildMissingConfig:
    def test_exits_one_when_config_file_does_not_exist(self, tmp_path):
        result = subprocess.run(
            [
                sys.executable,
                _SCRIPT,
                "build",
                "--platform",
                "linux",
                "--config",
                str(tmp_path / "nonexistent.toml"),
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 1


# ---------------------------------------------------------------------------
# Python version guard
# ---------------------------------------------------------------------------


class TestVersionGuard:
    def test_exits_with_readable_message_when_python_version_too_old(self, tmp_path):
        """Force-trigger the version guard by patching the condition to True."""
        source = Path(_SCRIPT).read_text(encoding="utf-8")
        # Replace the guard condition so it always fires regardless of Python version.
        patched = source.replace(
            "if sys.version_info < (3, 11):",
            "if True:  # patched — force trigger for test",
        )
        patched_script = tmp_path / "build_godot_version_test.py"
        patched_script.write_text(patched, encoding="utf-8")

        result = subprocess.run(
            [sys.executable, str(patched_script)],
            capture_output=True,
            text=True,
        )

        assert result.returncode != 0
        combined_output = result.stdout + result.stderr
        # The error message must mention the minimum required version.
        assert "3.11" in combined_output

    def test_guard_message_contains_interpreter_hint(self, tmp_path):
        """The version error should tell operators how to run the script correctly."""
        source = Path(_SCRIPT).read_text(encoding="utf-8")
        patched = source.replace(
            "if sys.version_info < (3, 11):",
            "if True:  # patched — force trigger for test",
        )
        patched_script = tmp_path / "build_godot_version_hint_test.py"
        patched_script.write_text(patched, encoding="utf-8")

        result = subprocess.run(
            [sys.executable, str(patched_script)],
            capture_output=True,
            text=True,
        )

        combined_output = result.stdout + result.stderr
        # The message must mention 'uv' (the recommended way to get the right Python).
        assert "uv" in combined_output


# ---------------------------------------------------------------------------
# build — --godot-repo official
# ---------------------------------------------------------------------------


class TestCliBuildGodotRepoOfficial:
    def test_godot_repo_official_is_accepted_without_exit_2(self, tmp_path):
        """--godot-repo official must be a valid option (argparse must not exit 2)."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(_MINIMAL_VALID_TOML, encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                _SCRIPT,
                "build",
                "--platform",
                "linux",
                "--godot-repo",
                "official",
                "--config",
                str(config_file),
                "--dry-run",
            ],
            capture_output=True,
            text=True,
        )

        # Exit 2 means argparse rejected the option.  Any other exit code
        # (0, 1, 3, 4 …) means the option was parsed successfully.
        assert result.returncode != 2, (
            f"--godot-repo official was rejected by argparse.\n"
            f"stderr: {result.stderr}"
        )


# ---------------------------------------------------------------------------
# build — new matrix flags
# ---------------------------------------------------------------------------


def _write_config(tmp_path) -> str:
    config_file = tmp_path / "config.toml"
    config_file.write_text(_MINIMAL_VALID_TOML, encoding="utf-8")
    return str(config_file)


# A macos/ios [[platforms]] entry used by the Apple-target tests. Apple
# targets always attempt to build — there is no host-side auto/force/skip
# mode and no best-effort fallback.
_APPLE_PLATFORMS_TOML = """\

[[platforms]]
name = "macos"
image = "ghcr.io/test/osx:4.3"
scons_flags = "platform=macos"
archs = ["universal"]

[[platforms]]
name = "ios"
image = "ghcr.io/test/ios:4.3"
scons_flags = "platform=ios"
archs = ["arm64"]
"""


def _write_config_apple(tmp_path) -> str:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        _MINIMAL_VALID_TOML + _APPLE_PLATFORMS_TOML,
        encoding="utf-8",
    )
    return str(config_file)


class TestCliBuildMatrixFlags:
    def test_flavor_kind_mono_arch_jobs_are_accepted_when_supplied(self, tmp_path):
        result = subprocess.run(
            [
                sys.executable,
                _SCRIPT,
                "build",
                "--platform",
                "linux",
                "--flavor",
                "release",
                "--kind",
                "editor",
                "--mono",
                "off",
                "--arch",
                "x86_64",
                "--jobs",
                "4",
                "--config",
                _write_config(tmp_path),
                "--dry-run",
            ],
            capture_output=True,
            text=True,
        )

        # Not rejected by argparse (exit 2 would mean a bad flag).
        assert result.returncode != 2, result.stderr

    def test_invalid_flavor_exits_one_when_derivation_fails(self, tmp_path):
        result = subprocess.run(
            [
                sys.executable,
                _SCRIPT,
                "build",
                "--platform",
                "linux",
                "--flavor",
                "nonsense",
                "--config",
                _write_config(tmp_path),
                "--dry-run",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 1

    def test_dry_run_emits_derived_scons_commands(self, tmp_path):
        result = subprocess.run(
            [
                sys.executable,
                _SCRIPT,
                "build",
                "--platform",
                "linux",
                "--flavor",
                "release",
                "--kind",
                "editor",
                "--mono",
                "off",
                "--arch",
                "x86_64",
                "--config",
                _write_config(tmp_path),
                "--dry-run",
            ],
            capture_output=True,
            text=True,
        )

        combined = result.stdout + result.stderr
        assert "target=editor" in combined
        assert "production=yes" in combined

    def test_mono_both_emits_classical_and_mono_commands(self, tmp_path):
        result = subprocess.run(
            [
                sys.executable,
                _SCRIPT,
                "build",
                "--platform",
                "linux",
                "--flavor",
                "release",
                "--kind",
                "editor",
                "--mono",
                "both",
                "--arch",
                "x86_64",
                "--config",
                _write_config(tmp_path),
                "--dry-run",
            ],
            capture_output=True,
            text=True,
        )

        combined = result.stdout + result.stderr
        # Both variants are derived: the Mono flag appears, and a classical
        # editor command (without it) is also present.
        assert "module_mono_enabled=yes" in combined
        assert combined.count("target=editor") >= 2


# ---------------------------------------------------------------------------
# build — Apple targets always build (no host-side skip / auto-detection)
# ---------------------------------------------------------------------------


class TestCliBuildAppleAlwaysBuilds:
    def test_macos_build_proceeds_to_scons_derivation(self, tmp_path):
        # macOS must derive real SCons commands in dry-run; no graceful skip.
        result = subprocess.run(
            [
                sys.executable,
                _SCRIPT,
                "build",
                "--platform",
                "macos",
                "--config",
                _write_config_apple(tmp_path),
                "--dry-run",
            ],
            capture_output=True,
            text=True,
        )

        combined = result.stdout + result.stderr
        assert "skipping apple target" not in combined.lower()
        assert "platform=macos" in combined
        assert "target=editor" in combined

    def test_ios_build_proceeds_to_scons_derivation(self, tmp_path):
        result = subprocess.run(
            [
                sys.executable,
                _SCRIPT,
                "build",
                "--platform",
                "ios",
                "--config",
                _write_config_apple(tmp_path),
                "--dry-run",
            ],
            capture_output=True,
            text=True,
        )

        combined = result.stdout + result.stderr
        assert "skipping apple target" not in combined.lower()
        assert "platform=ios" in combined


# ---------------------------------------------------------------------------
# release sub-command
# ---------------------------------------------------------------------------


class TestCliRelease:
    def test_release_subcommand_is_registered(self):
        result = _run_cli("--help")

        assert "release" in result.stdout

    def test_release_dry_run_prints_gh_release_command(self, tmp_path):
        result = subprocess.run(
            [
                sys.executable,
                _SCRIPT,
                "release",
                "--config",
                _write_config(tmp_path),
                "--jobs",
                "10",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        combined = result.stdout + result.stderr
        assert "gh release" in combined
        assert "--prerelease" in combined

    def test_release_no_upload_stops_before_publish(self, tmp_path):
        result = subprocess.run(
            [
                sys.executable,
                _SCRIPT,
                "release",
                "--config",
                _write_config(tmp_path),
                "--no-upload",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        combined = result.stdout + result.stderr
        assert "gh release" not in combined

    def test_release_no_nuget_skips_nuget_publish(self, tmp_path):
        result = subprocess.run(
            [
                sys.executable,
                _SCRIPT,
                "release",
                "--config",
                _write_config(tmp_path),
                "--no-upload",
                "--no-nuget",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        combined = result.stdout + result.stderr
        assert "dotnet nuget push" not in combined
        assert "Skipping NuGet publish" in combined


# ---------------------------------------------------------------------------
# containers — --extract-sdks-only
# ---------------------------------------------------------------------------


class TestCliContainersExtractSdksOnly:
    """The --extract-sdks-only flag short-circuits to extract_apple_sdks."""

    def test_parser_accepts_extract_sdks_only_without_type(self, tmp_path):
        """argparse must not require --type when --extract-sdks-only is given."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(_MINIMAL_VALID_TOML, encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                _SCRIPT,
                "containers",
                "--extract-sdks-only",
                "--config",
                str(config_file),
                "--dry-run",
            ],
            capture_output=True,
            text=True,
        )

        # argparse rejection -> exit 2. We accept any exit code except 2; the
        # extraction itself may exit 4 in dry-run if the godot-xcode image is
        # absent locally, but argparse must have accepted the flags first.
        assert result.returncode != 2, (
            f"--extract-sdks-only was rejected by argparse.\n"
            f"stderr: {result.stderr}"
        )

    def test_cmd_containers_routes_to_extract_apple_sdks(self, tmp_path):
        """When --extract-sdks-only is set, cmd_containers must call
        extract_apple_sdks with force=True and skip build_and_push entirely."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(_MINIMAL_VALID_TOML, encoding="utf-8")

        module = _import_build_godot()

        # argparse Namespace with the same field set the real parser produces.
        class Args:
            extract_sdks_only = True
            push = False
            type = None
            version = None
            config = str(config_file)
            dry_run = True

        extract_calls: list[dict] = []
        build_calls: list[dict] = []

        def fake_extract(**kwargs):
            extract_calls.append(kwargs)
            return 0

        def fake_build_and_push(**kwargs):
            build_calls.append(kwargs)
            return 0

        with (
            patch.object(module, "extract_apple_sdks", side_effect=fake_extract),
            patch.object(module, "build_and_push", side_effect=fake_build_and_push),
        ):
            rc = module.cmd_containers(Args())

        assert rc == 0
        assert len(extract_calls) == 1
        assert extract_calls[0]["force"] is True
        assert extract_calls[0]["dry_run"] is True
        # Must NOT have invoked the build pipeline.
        assert build_calls == []

    def test_cmd_containers_rejects_extract_sdks_only_with_push(self, tmp_path):
        """--extract-sdks-only and --push are mutually exclusive."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(_MINIMAL_VALID_TOML, encoding="utf-8")

        module = _import_build_godot()

        class Args:
            extract_sdks_only = True
            push = True
            type = None
            version = None
            config = str(config_file)
            dry_run = True

        with (
            patch.object(module, "extract_apple_sdks") as mock_extract,
            patch.object(module, "build_and_push") as mock_build,
        ):
            rc = module.cmd_containers(Args())

        assert rc == 1
        mock_extract.assert_not_called()
        mock_build.assert_not_called()

    def test_cmd_containers_errors_when_type_missing_and_not_extract_only(
        self, tmp_path
    ):
        """The original --type requirement is preserved for the build flow."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(_MINIMAL_VALID_TOML, encoding="utf-8")

        module = _import_build_godot()

        class Args:
            extract_sdks_only = False
            push = False
            type = None
            version = None
            config = str(config_file)
            dry_run = True

        with (
            patch.object(module, "extract_apple_sdks") as mock_extract,
            patch.object(module, "build_and_push") as mock_build,
        ):
            rc = module.cmd_containers(Args())

        assert rc == 1
        mock_extract.assert_not_called()
        mock_build.assert_not_called()
