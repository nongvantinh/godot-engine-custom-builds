"""Tests for build-godot.py CLI — argument parsing, exit codes, and guards."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = str(Path(__file__).parent.parent / "build-godot.py")

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
