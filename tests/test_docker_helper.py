"""Tests for scripts/docker_helper.py — Docker interaction helpers."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from scripts.config import ConfigError
from scripts.docker_helper import DockerUnavailableError, ensure_docker, login, run_build

# ---------------------------------------------------------------------------
# ensure_docker
# ---------------------------------------------------------------------------


class TestEnsureDocker:
    def test_raises_docker_unavailable_when_docker_binary_not_on_path(self):
        with patch("scripts.docker_helper.shutil.which", return_value=None):
            with pytest.raises(DockerUnavailableError, match="docker binary"):
                ensure_docker()

    def test_raises_docker_unavailable_when_docker_daemon_not_running(self):
        mock_result = MagicMock()
        mock_result.returncode = 1

        with patch("scripts.docker_helper.shutil.which", return_value="/usr/bin/docker"):
            with patch("scripts.docker_helper.subprocess.run", return_value=mock_result):
                with pytest.raises(DockerUnavailableError, match="daemon"):
                    ensure_docker()

    def test_succeeds_silently_when_docker_is_available(self):
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("scripts.docker_helper.shutil.which", return_value="/usr/bin/docker"):
            with patch("scripts.docker_helper.subprocess.run", return_value=mock_result):
                ensure_docker()  # Must not raise


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


class TestLogin:
    def test_raises_config_error_when_ghcr_pat_env_var_not_set(self, monkeypatch):
        monkeypatch.delenv("GHCR_PAT", raising=False)

        with pytest.raises(ConfigError, match="GHCR_PAT"):
            login("ghcr.io", "testuser")

    def test_invokes_docker_login_with_pat_read_from_env(self, monkeypatch):
        monkeypatch.setenv("GHCR_PAT", "my_test_pat")

        with patch("scripts.docker_helper.subprocess.run") as mock_run:
            login("ghcr.io", "testuser")

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "docker" in cmd
        assert "login" in cmd
        assert "ghcr.io" in cmd
        assert "--username" in cmd
        assert "testuser" in cmd
        # PAT must be delivered via stdin, not as a CLI argument.
        assert mock_run.call_args[1]["input"] == b"my_test_pat"

    def test_dry_run_skips_subprocess_without_raising(self, monkeypatch):
        monkeypatch.setenv("GHCR_PAT", "my_test_pat")

        with patch("scripts.docker_helper.subprocess.run") as mock_run:
            login("ghcr.io", "testuser", dry_run=True)

        mock_run.assert_not_called()

    def test_uses_provided_registry_and_username_in_docker_command(self, monkeypatch):
        monkeypatch.setenv("GHCR_PAT", "token123")

        with patch("scripts.docker_helper.subprocess.run") as mock_run:
            login("myregistry.example.com", "alice")

        cmd = mock_run.call_args[0][0]
        assert "myregistry.example.com" in cmd
        assert "alice" in cmd


# ---------------------------------------------------------------------------
# run_build
# ---------------------------------------------------------------------------


class TestRunBuild:
    def test_returns_zero_when_docker_subprocess_succeeds(self):
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("scripts.docker_helper.subprocess.run", return_value=mock_result):
            rc = run_build(
                image="ghcr.io/test/linux:4.3",
                scons_flags="platform=linuxbsd target=editor",
                source_dir="/tmp/godot",
                output_dir="/tmp/out",
            )

        assert rc == 0

    def test_returns_non_zero_exit_code_propagated_from_docker_subprocess(self):
        mock_result = MagicMock()
        mock_result.returncode = 42

        with patch("scripts.docker_helper.subprocess.run", return_value=mock_result):
            rc = run_build(
                image="ghcr.io/test/linux:4.3",
                scons_flags="platform=linuxbsd",
                source_dir="/tmp/godot",
                output_dir="/tmp/out",
            )

        assert rc == 42

    def test_dry_run_returns_zero_without_calling_subprocess(self):
        with patch("scripts.docker_helper.subprocess.run") as mock_run:
            rc = run_build(
                image="ghcr.io/test/linux:4.3",
                scons_flags="platform=linuxbsd",
                source_dir="/tmp/godot",
                output_dir="/tmp/out",
                dry_run=True,
            )

        mock_run.assert_not_called()
        assert rc == 0

    def test_mounts_source_and_output_dirs_as_docker_volumes(self):
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("scripts.docker_helper.subprocess.run", return_value=mock_result) as mock_run:
            run_build(
                image="ghcr.io/test/linux:4.3",
                scons_flags="platform=linuxbsd",
                source_dir="/absolute/godot",
                output_dir="/absolute/out",
            )

        cmd = mock_run.call_args[0][0]
        cmd_str = " ".join(cmd)
        assert "/absolute/godot:/root/godot" in cmd_str
        assert "/absolute/out:/root/out" in cmd_str

    def test_passes_scons_flags_as_trailing_arguments_to_container(self):
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("scripts.docker_helper.subprocess.run", return_value=mock_result) as mock_run:
            run_build(
                image="ghcr.io/test/linux:4.3",
                scons_flags="platform=linuxbsd target=editor",
                source_dir="/tmp/godot",
                output_dir="/tmp/out",
            )

        cmd = mock_run.call_args[0][0]
        # Flags are embedded in the bash -c shell script string.
        shell_arg = cmd[cmd.index("-c") + 1]
        assert "platform=linuxbsd" in shell_arg
        assert "target=editor" in shell_arg

    def test_env_setup_wraps_command_in_bash(self):
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("scripts.docker_helper.subprocess.run", return_value=mock_result) as mock_run:
            run_build(
                image="ghcr.io/test/linux:4.3",
                scons_flags="platform=linuxbsd target=editor",
                source_dir="/tmp/godot",
                output_dir="/tmp/out",
                env_setup="export PATH=$GODOT_SDK_LINUX_X86_64/bin:$BASE_PATH",
            )

        cmd = mock_run.call_args[0][0]
        assert "bash" in cmd
        assert "-c" in cmd
        shell_arg = cmd[cmd.index("-c") + 1]
        assert "export PATH=$GODOT_SDK_LINUX_X86_64/bin:$BASE_PATH" in shell_arg
        assert "scons platform=linuxbsd target=editor" in shell_arg

    def test_no_env_setup_does_not_wrap_in_bash(self):
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("scripts.docker_helper.subprocess.run", return_value=mock_result) as mock_run:
            run_build(
                image="ghcr.io/test/linux:4.3",
                scons_flags="platform=linuxbsd",
                source_dir="/tmp/godot",
                output_dir="/tmp/out",
            )

        cmd = mock_run.call_args[0][0]
        # Always uses bash -c for consistent chaining of the copy step.
        assert "bash" in cmd
        shell_arg = cmd[cmd.index("-c") + 1]
        assert "scons platform=linuxbsd" in shell_arg
        assert "cp -rvp bin/godot*" in shell_arg
