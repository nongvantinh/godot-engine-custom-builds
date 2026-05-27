"""Tests for scripts/container_builder.py — container build/push helpers."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from scripts.container_builder import (
    SUPPORTED_TYPES,
    ContainerBuildError,
    UnsupportedTypeError,
    build_and_push,
    build_image,
    is_image_built,
    push_image,
)

_CONTAINERS_DIR = Path("/fake/containers")
_REGISTRY = "ghcr.io"
_USERNAME = "testuser"
_VERSION = "4.7"


# ---------------------------------------------------------------------------
# TestBuildImage
# ---------------------------------------------------------------------------


class TestBuildImage:
    def test_dry_run_logs_correct_build_command_for_linux(self, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="scripts.container_builder"):
            build_image(
                container_type="linux",
                version=_VERSION,
                containers_dir=_CONTAINERS_DIR,
                registry=_REGISTRY,
                username=_USERNAME,
                dry_run=True,
            )

        assert any("dry-run" in record.message for record in caplog.records)
        assert any("godot-linux:4.7" in record.message for record in caplog.records)

    def test_dry_run_does_not_call_subprocess(self):
        with patch("scripts.container_builder.subprocess.run") as mock_run:
            build_image(
                container_type="linux",
                version=_VERSION,
                containers_dir=_CONTAINERS_DIR,
                registry=_REGISTRY,
                username=_USERNAME,
                dry_run=True,
            )
        mock_run.assert_not_called()

    def test_unsupported_type_raises_unsupported_type_error(self):
        with pytest.raises(UnsupportedTypeError, match="unsupported_type"):
            build_image(
                container_type="unsupported_type",
                version=_VERSION,
                containers_dir=_CONTAINERS_DIR,
                registry=_REGISTRY,
                username=_USERNAME,
            )

    def test_base_type_uses_dockerfile_base_without_build_arg(self):
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("scripts.container_builder.subprocess.run", return_value=mock_result) as mock_run:
            build_image(
                container_type="base",
                version=_VERSION,
                containers_dir=_CONTAINERS_DIR,
                registry=_REGISTRY,
                username=_USERNAME,
            )

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        cmd_str = " ".join(cmd)
        assert "Dockerfile.base" in cmd_str
        assert "godot-fedora:4.7" in cmd_str
        # base must NOT pass --build-arg IMAGE_VERSION
        assert "--build-arg" not in cmd_str

    def test_linux_type_uses_dockerfile_linux_with_build_arg(self):
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("scripts.container_builder.subprocess.run", return_value=mock_result) as mock_run:
            build_image(
                container_type="linux",
                version=_VERSION,
                containers_dir=_CONTAINERS_DIR,
                registry=_REGISTRY,
                username=_USERNAME,
            )

        cmd = mock_run.call_args[0][0]
        cmd_str = " ".join(cmd)
        assert "Dockerfile.linux" in cmd_str
        assert "godot-linux:4.7" in cmd_str
        assert f"IMAGE_VERSION={_VERSION}" in cmd_str

    def test_failed_docker_build_raises_container_build_error(self):
        with patch("scripts.container_builder.subprocess.run") as mock_run:
            mock_run.side_effect = __import__("subprocess").CalledProcessError(1, "docker")
            with pytest.raises(ContainerBuildError, match="linux"):
                build_image(
                    container_type="linux",
                    version=_VERSION,
                    containers_dir=_CONTAINERS_DIR,
                    registry=_REGISTRY,
                    username=_USERNAME,
                )

    @pytest.mark.parametrize("container_type", sorted(SUPPORTED_TYPES))
    def test_all_supported_types_build_without_error(self, container_type):
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("scripts.container_builder.subprocess.run", return_value=mock_result):
            # Should not raise
            build_image(
                container_type=container_type,
                version=_VERSION,
                containers_dir=_CONTAINERS_DIR,
                registry=_REGISTRY,
                username=_USERNAME,
                dry_run=True,
            )


# ---------------------------------------------------------------------------
# TestPushImage
# ---------------------------------------------------------------------------


class TestPushImage:
    def test_dry_run_logs_tag_and_push_commands(self, caplog, monkeypatch):
        import logging

        monkeypatch.setenv("GHCR_PAT", "fake_pat")

        with caplog.at_level(logging.INFO, logger="scripts.container_builder"):
            push_image(
                container_type="linux",
                version=_VERSION,
                registry=_REGISTRY,
                username=_USERNAME,
                dry_run=True,
            )

        messages = " ".join(r.message for r in caplog.records)
        assert "dry-run" in messages
        assert "godot-linux:4.7" in messages

    def test_dry_run_does_not_call_subprocess(self, monkeypatch):
        monkeypatch.setenv("GHCR_PAT", "fake_pat")

        with patch("scripts.container_builder.subprocess.run") as mock_run:
            push_image(
                container_type="linux",
                version=_VERSION,
                registry=_REGISTRY,
                username=_USERNAME,
                dry_run=True,
            )

        mock_run.assert_not_called()

    def test_missing_ghcr_pat_logs_warning_and_skips_without_raising(
        self, monkeypatch, caplog
    ):
        import logging

        monkeypatch.delenv("GHCR_PAT", raising=False)

        with patch("scripts.container_builder.subprocess.run") as mock_run:
            with caplog.at_level(logging.WARNING, logger="scripts.container_builder"):
                push_image(
                    container_type="linux",
                    version=_VERSION,
                    registry=_REGISTRY,
                    username=_USERNAME,
                )

        mock_run.assert_not_called()
        assert any("GHCR_PAT" in record.message for record in caplog.records)

    def test_push_uses_correct_remote_tag_format(self, monkeypatch):
        monkeypatch.setenv("GHCR_PAT", "fake_pat")

        calls_made: list = []

        def fake_run(cmd, **kwargs):
            calls_made.append(cmd)
            result = MagicMock()
            result.returncode = 0
            return result

        with patch("scripts.container_builder.subprocess.run", side_effect=fake_run):
            push_image(
                container_type="linux",
                version=_VERSION,
                registry=_REGISTRY,
                username=_USERNAME,
            )

        # Collect all command strings
        all_cmds = [" ".join(c) for c in calls_made]

        # docker tag command
        tag_cmd = next((c for c in all_cmds if "tag" in c), None)
        assert tag_cmd is not None
        assert "godot-linux:4.7" in tag_cmd
        assert f"{_REGISTRY}/{_USERNAME}/godot-linux:4.7" in tag_cmd

        # docker push command
        push_cmd = next((c for c in all_cmds if "push" in c), None)
        assert push_cmd is not None
        assert f"{_REGISTRY}/{_USERNAME}/godot-linux:4.7" in push_cmd

    def test_unsupported_type_raises_unsupported_type_error(self, monkeypatch):
        monkeypatch.setenv("GHCR_PAT", "fake_pat")
        with pytest.raises(UnsupportedTypeError):
            push_image(
                container_type="osx",
                version=_VERSION,
                registry=_REGISTRY,
                username=_USERNAME,
            )

    def test_base_type_push_uses_godot_fedora_image_name(self, monkeypatch):
        monkeypatch.setenv("GHCR_PAT", "fake_pat")

        calls_made: list = []

        def fake_run(cmd, **kwargs):
            calls_made.append(cmd)
            result = MagicMock()
            result.returncode = 0
            return result

        with patch("scripts.container_builder.subprocess.run", side_effect=fake_run):
            push_image(
                container_type="base",
                version=_VERSION,
                registry=_REGISTRY,
                username=_USERNAME,
            )

        all_cmds = [" ".join(c) for c in calls_made]
        tag_cmd = next((c for c in all_cmds if "tag" in c), None)
        assert tag_cmd is not None
        assert "godot-fedora:4.7" in tag_cmd
        assert f"{_REGISTRY}/{_USERNAME}/godot-fedora:4.7" in tag_cmd


# ---------------------------------------------------------------------------
# TestIsImageBuilt
# ---------------------------------------------------------------------------


class TestIsImageBuilt:
    def test_returns_false_when_image_not_in_docker_output(self):
        mock_result = MagicMock()
        mock_result.stdout = "ubuntu:22.04\ndebian:bullseye\n"

        with patch("scripts.container_builder.subprocess.run", return_value=mock_result):
            assert is_image_built("linux", _VERSION) is False

    def test_returns_true_when_image_present_in_docker_output(self):
        mock_result = MagicMock()
        mock_result.stdout = f"godot-linux:{_VERSION}\nubuntu:22.04\n"

        with patch("scripts.container_builder.subprocess.run", return_value=mock_result):
            assert is_image_built("linux", _VERSION) is True

    def test_returns_false_for_unsupported_type_without_raising(self):
        assert is_image_built("osx", _VERSION) is False

    def test_returns_true_for_base_type_using_godot_fedora_name(self):
        mock_result = MagicMock()
        mock_result.stdout = f"godot-fedora:{_VERSION}\n"

        with patch("scripts.container_builder.subprocess.run", return_value=mock_result):
            assert is_image_built("base", _VERSION) is True

    def test_returns_false_when_version_tag_does_not_match(self):
        mock_result = MagicMock()
        mock_result.stdout = "godot-linux:4.3\n"

        with patch("scripts.container_builder.subprocess.run", return_value=mock_result):
            assert is_image_built("linux", _VERSION) is False


# ---------------------------------------------------------------------------
# TestBuildAndPush
# ---------------------------------------------------------------------------


class TestBuildAndPush:
    def _make_fake_subprocess(self):
        """Return a fake subprocess.run that always succeeds."""
        mock = MagicMock()
        mock.return_value.returncode = 0
        mock.return_value.stdout = ""  # no images found → always rebuild
        return mock

    def test_all_type_expands_to_all_five_types(self, monkeypatch):
        """``--type all`` must result in exactly 5 build_image calls."""
        built: list[str] = []

        def fake_build_image(container_type, **kwargs):
            built.append(container_type)

        with patch("scripts.container_builder.build_image", side_effect=fake_build_image):
            with patch("scripts.container_builder.is_image_built", return_value=False):
                with patch("scripts.container_builder.shutil.which", return_value="/usr/bin/docker"):
                    rc = build_and_push(
                        types=["all"],
                        version=_VERSION,
                        containers_dir=_CONTAINERS_DIR,
                        registry=_REGISTRY,
                        username=_USERNAME,
                        push=False,
                        dry_run=False,
                    )

        assert rc == 0
        assert set(built) == SUPPORTED_TYPES

    def test_base_is_built_before_any_platform_type(self, monkeypatch):
        """``base`` must appear first in the build sequence."""
        built_order: list[str] = []

        def fake_build_image(container_type, **kwargs):
            built_order.append(container_type)

        with patch("scripts.container_builder.build_image", side_effect=fake_build_image):
            with patch("scripts.container_builder.is_image_built", return_value=False):
                with patch("scripts.container_builder.shutil.which", return_value="/usr/bin/docker"):
                    rc = build_and_push(
                        types=["linux", "windows"],
                        version=_VERSION,
                        containers_dir=_CONTAINERS_DIR,
                        registry=_REGISTRY,
                        username=_USERNAME,
                        push=False,
                        dry_run=False,
                    )

        assert rc == 0
        assert "base" in built_order
        assert built_order.index("base") < built_order.index("linux")
        assert built_order.index("base") < built_order.index("windows")

    def test_base_only_request_does_not_duplicate_base(self):
        """When only ``base`` is requested, it is built exactly once."""
        built: list[str] = []

        def fake_build_image(container_type, **kwargs):
            built.append(container_type)

        with patch("scripts.container_builder.build_image", side_effect=fake_build_image):
            with patch("scripts.container_builder.is_image_built", return_value=False):
                with patch("scripts.container_builder.shutil.which", return_value="/usr/bin/docker"):
                    rc = build_and_push(
                        types=["base"],
                        version=_VERSION,
                        containers_dir=_CONTAINERS_DIR,
                        registry=_REGISTRY,
                        username=_USERNAME,
                        push=False,
                        dry_run=False,
                    )

        assert rc == 0
        assert built.count("base") == 1

    def test_unsupported_type_returns_exit_code_2(self):
        with patch("scripts.container_builder.shutil.which", return_value="/usr/bin/docker"):
            rc = build_and_push(
                types=["osx"],
                version=_VERSION,
                containers_dir=_CONTAINERS_DIR,
                registry=_REGISTRY,
                username=_USERNAME,
                push=False,
                dry_run=False,
            )
        assert rc == 2

    def test_docker_unavailable_returns_exit_code_3(self):
        with patch("scripts.container_builder.shutil.which", return_value=None):
            rc = build_and_push(
                types=["linux"],
                version=_VERSION,
                containers_dir=_CONTAINERS_DIR,
                registry=_REGISTRY,
                username=_USERNAME,
                push=False,
                dry_run=False,
            )
        assert rc == 3

    def test_build_failure_returns_exit_code_4(self):
        with patch("scripts.container_builder.shutil.which", return_value="/usr/bin/docker"):
            with patch("scripts.container_builder.is_image_built", return_value=False):
                with patch(
                    "scripts.container_builder.build_image",
                    side_effect=ContainerBuildError("boom"),
                ):
                    rc = build_and_push(
                        types=["linux"],
                        version=_VERSION,
                        containers_dir=_CONTAINERS_DIR,
                        registry=_REGISTRY,
                        username=_USERNAME,
                        push=False,
                        dry_run=False,
                    )
        assert rc == 4

    def test_push_flag_calls_push_image_for_each_type(self, monkeypatch):
        monkeypatch.setenv("GHCR_PAT", "fake_pat")
        pushed: list[str] = []

        def fake_push_image(container_type, **kwargs):
            pushed.append(container_type)

        with patch("scripts.container_builder.build_image"):
            with patch("scripts.container_builder.push_image", side_effect=fake_push_image):
                with patch("scripts.container_builder.is_image_built", return_value=False):
                    with patch("scripts.container_builder.shutil.which", return_value="/usr/bin/docker"):
                        rc = build_and_push(
                            types=["linux"],
                            version=_VERSION,
                            containers_dir=_CONTAINERS_DIR,
                            registry=_REGISTRY,
                            username=_USERNAME,
                            push=True,
                            dry_run=False,
                        )

        assert rc == 0
        assert "base" in pushed
        assert "linux" in pushed

    def test_skip_build_when_image_already_exists_locally(self):
        built: list[str] = []

        def fake_build_image(container_type, **kwargs):
            built.append(container_type)

        with patch("scripts.container_builder.build_image", side_effect=fake_build_image):
            with patch("scripts.container_builder.is_image_built", return_value=True):
                with patch("scripts.container_builder.shutil.which", return_value="/usr/bin/docker"):
                    rc = build_and_push(
                        types=["linux"],
                        version=_VERSION,
                        containers_dir=_CONTAINERS_DIR,
                        registry=_REGISTRY,
                        username=_USERNAME,
                        push=False,
                        dry_run=False,
                    )

        assert rc == 0
        assert built == []  # nothing built because images exist

    def test_canonical_build_order_for_all_types(self):
        """Types must be built in the canonical order: base → linux → windows → android → web."""
        built_order: list[str] = []

        def fake_build_image(container_type, **kwargs):
            built_order.append(container_type)

        with patch("scripts.container_builder.build_image", side_effect=fake_build_image):
            with patch("scripts.container_builder.is_image_built", return_value=False):
                with patch("scripts.container_builder.shutil.which", return_value="/usr/bin/docker"):
                    build_and_push(
                        types=["all"],
                        version=_VERSION,
                        containers_dir=_CONTAINERS_DIR,
                        registry=_REGISTRY,
                        username=_USERNAME,
                        push=False,
                        dry_run=False,
                    )

        expected_order = ["base", "linux", "windows", "android", "web"]
        assert built_order == expected_order
