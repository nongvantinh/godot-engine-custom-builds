"""Tests for scripts/container_builder.py — container build/push helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.container_builder import (
    SUPPORTED_TYPES,
    ContainerBuildError,
    UnsupportedTypeError,
    build_and_push,
    build_image,
    extract_apple_sdks,
    is_image_built,
    push_image,
)

_CONTAINERS_DIR = Path("/fake/containers")
_REGISTRY = "ghcr.io"
_USERNAME = "testuser"
_VERSION = "4.8"


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
        assert any("godot-linux:4.8" in record.message for record in caplog.records)

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

        with patch(
            "scripts.container_builder.subprocess.run", return_value=mock_result
        ) as mock_run:
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
        assert "godot-fedora:4.8" in cmd_str
        # base must NOT pass --build-arg IMAGE_VERSION
        assert "--build-arg" not in cmd_str

    def test_linux_type_uses_dockerfile_linux_with_build_arg(self):
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch(
            "scripts.container_builder.subprocess.run", return_value=mock_result
        ) as mock_run:
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
        assert "godot-linux:4.8" in cmd_str
        assert f"IMAGE_VERSION={_VERSION}" in cmd_str

    def test_failed_docker_build_raises_container_build_error(self):
        with patch("scripts.container_builder.subprocess.run") as mock_run:
            mock_run.side_effect = __import__("subprocess").CalledProcessError(
                1, "docker"
            )
            with pytest.raises(ContainerBuildError, match="linux"):
                build_image(
                    container_type="linux",
                    version=_VERSION,
                    containers_dir=_CONTAINERS_DIR,
                    registry=_REGISTRY,
                    username=_USERNAME,
                )

    @pytest.mark.parametrize("apple_type", ["xcode", "osx", "ios"])
    def test_apple_image_passes_sdk_version_build_args_from_config(self, apple_type):
        # Config is the single source of truth for the SDK versions at
        # image-build time — container_builder passes --build-arg XCODE_SDKV /
        # APPLE_SDKV (alongside IMAGE_VERSION) for the Apple image types.
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch(
            "scripts.container_builder.subprocess.run", return_value=mock_result
        ) as mock_run:
            build_image(
                container_type=apple_type,
                version=_VERSION,
                containers_dir=_CONTAINERS_DIR,
                registry=_REGISTRY,
                username=_USERNAME,
                xcode_sdkv="26.1.1",
                apple_sdkv="26.1",
            )

        cmd = mock_run.call_args[0][0]
        cmd_str = " ".join(cmd)
        assert f"IMAGE_VERSION={_VERSION}" in cmd_str
        assert "--build-arg XCODE_SDKV=26.1.1" in cmd_str
        assert "--build-arg APPLE_SDKV=26.1" in cmd_str

    def test_non_apple_image_does_not_pass_sdk_version_build_args(self):
        # The SDK version build-args belong only on the Apple Dockerfiles; a
        # linux build must not receive them even when values are supplied.
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch(
            "scripts.container_builder.subprocess.run", return_value=mock_result
        ) as mock_run:
            build_image(
                container_type="linux",
                version=_VERSION,
                containers_dir=_CONTAINERS_DIR,
                registry=_REGISTRY,
                username=_USERNAME,
                xcode_sdkv="26.1.1",
                apple_sdkv="26.1",
            )

        cmd_str = " ".join(mock_run.call_args[0][0])
        assert "XCODE_SDKV" not in cmd_str
        assert "APPLE_SDKV" not in cmd_str

    def test_apple_image_omits_sdk_build_args_when_versions_unset(self):
        # A standalone build with no config versions falls back to the Dockerfile
        # ENV defaults — container_builder passes no SDK --build-arg.
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch(
            "scripts.container_builder.subprocess.run", return_value=mock_result
        ) as mock_run:
            build_image(
                container_type="osx",
                version=_VERSION,
                containers_dir=_CONTAINERS_DIR,
                registry=_REGISTRY,
                username=_USERNAME,
            )

        cmd_str = " ".join(mock_run.call_args[0][0])
        assert "XCODE_SDKV" not in cmd_str
        assert "APPLE_SDKV" not in cmd_str

    @pytest.mark.parametrize("container_type", sorted(SUPPORTED_TYPES))
    def test_all_supported_types_build_without_error(self, container_type):
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch(
            "scripts.container_builder.subprocess.run", return_value=mock_result
        ):
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
        assert "godot-linux:4.8" in messages

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
        assert "godot-linux:4.8" in tag_cmd
        assert f"{_REGISTRY}/{_USERNAME}/godot-linux:4.8" in tag_cmd

        # docker push command
        push_cmd = next((c for c in all_cmds if "push" in c), None)
        assert push_cmd is not None
        assert f"{_REGISTRY}/{_USERNAME}/godot-linux:4.8" in push_cmd

    def test_unsupported_type_raises_unsupported_type_error(self, monkeypatch):
        monkeypatch.setenv("GHCR_PAT", "fake_pat")
        with pytest.raises(UnsupportedTypeError):
            push_image(
                container_type="solaris",
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
        assert "godot-fedora:4.8" in tag_cmd
        assert f"{_REGISTRY}/{_USERNAME}/godot-fedora:4.8" in tag_cmd


# ---------------------------------------------------------------------------
# TestIsImageBuilt
# ---------------------------------------------------------------------------


class TestIsImageBuilt:
    def test_returns_false_when_image_not_in_docker_output(self):
        mock_result = MagicMock()
        mock_result.stdout = "ubuntu:22.04\ndebian:bullseye\n"

        with patch(
            "scripts.container_builder.subprocess.run", return_value=mock_result
        ):
            assert is_image_built("linux", _VERSION) is False

    def test_returns_true_when_image_present_in_docker_output(self):
        mock_result = MagicMock()
        mock_result.stdout = f"godot-linux:{_VERSION}\nubuntu:22.04\n"

        with patch(
            "scripts.container_builder.subprocess.run", return_value=mock_result
        ):
            assert is_image_built("linux", _VERSION) is True

    def test_returns_false_for_unsupported_type_without_raising(self):
        # ``"osx"`` is a real supported type, so use a sentinel that will
        # never be a real platform.
        assert is_image_built("definitely-not-a-platform", _VERSION) is False

    def test_returns_true_for_base_type_using_godot_fedora_name(self):
        mock_result = MagicMock()
        mock_result.stdout = f"godot-fedora:{_VERSION}\n"

        with patch(
            "scripts.container_builder.subprocess.run", return_value=mock_result
        ):
            assert is_image_built("base", _VERSION) is True

    def test_returns_false_when_version_tag_does_not_match(self):
        mock_result = MagicMock()
        mock_result.stdout = "godot-linux:4.3\n"

        with patch(
            "scripts.container_builder.subprocess.run", return_value=mock_result
        ):
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

    def test_all_type_expands_to_every_supported_type(self, monkeypatch):
        """``--type all`` must build exactly the supported set (incl. Apple)."""
        built: list[str] = []

        def fake_build_image(container_type, **kwargs):
            built.append(container_type)

        with (
            patch(
                "scripts.container_builder.build_image", side_effect=fake_build_image
            ),
            patch("scripts.container_builder.extract_apple_sdks", return_value=0),
        ):
            with patch("scripts.container_builder.is_image_built", return_value=False):
                with patch(
                    "scripts.container_builder.shutil.which",
                    return_value="/usr/bin/docker",
                ):
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

        with patch(
            "scripts.container_builder.build_image", side_effect=fake_build_image
        ):
            with patch("scripts.container_builder.is_image_built", return_value=False):
                with patch(
                    "scripts.container_builder.shutil.which",
                    return_value="/usr/bin/docker",
                ):
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

        with patch(
            "scripts.container_builder.build_image", side_effect=fake_build_image
        ):
            with patch("scripts.container_builder.is_image_built", return_value=False):
                with patch(
                    "scripts.container_builder.shutil.which",
                    return_value="/usr/bin/docker",
                ):
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
        with patch(
            "scripts.container_builder.shutil.which", return_value="/usr/bin/docker"
        ):
            rc = build_and_push(
                types=["solaris"],
                version=_VERSION,
                containers_dir=_CONTAINERS_DIR,
                registry=_REGISTRY,
                username=_USERNAME,
                push=False,
                dry_run=False,
            )
        assert rc == 2

    def test_apple_types_are_supported(self):
        # Directive 2: xcode/osx/ios are now buildable container types.
        assert {"xcode", "osx", "ios"} <= SUPPORTED_TYPES

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
        with patch(
            "scripts.container_builder.shutil.which", return_value="/usr/bin/docker"
        ):
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
            with patch(
                "scripts.container_builder.push_image", side_effect=fake_push_image
            ):
                with patch(
                    "scripts.container_builder.is_image_built", return_value=False
                ):
                    with patch(
                        "scripts.container_builder.shutil.which",
                        return_value="/usr/bin/docker",
                    ):
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

        with patch(
            "scripts.container_builder.build_image", side_effect=fake_build_image
        ):
            with patch("scripts.container_builder.is_image_built", return_value=True):
                with patch(
                    "scripts.container_builder.shutil.which",
                    return_value="/usr/bin/docker",
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

        assert rc == 0
        assert built == []  # nothing built because images exist

    def test_canonical_build_order_for_all_types(self):
        """Types must be built in canonical order, with the Apple chain last
        (xcode → osx → ios) because osx consumes xcode's SDK tarballs and ios is
        FROM godot-osx (Directive 2)."""
        built_order: list[str] = []

        def fake_build_image(container_type, **kwargs):
            built_order.append(container_type)

        with (
            patch(
                "scripts.container_builder.build_image", side_effect=fake_build_image
            ),
            patch("scripts.container_builder.extract_apple_sdks", return_value=0),
        ):
            with patch("scripts.container_builder.is_image_built", return_value=False):
                with patch(
                    "scripts.container_builder.shutil.which",
                    return_value="/usr/bin/docker",
                ):
                    build_and_push(
                        types=["all"],
                        version=_VERSION,
                        containers_dir=_CONTAINERS_DIR,
                        registry=_REGISTRY,
                        username=_USERNAME,
                        push=False,
                        dry_run=False,
                    )

        expected_order = [
            "base",
            "linux",
            "windows",
            "android",
            "web",
            "xcode",
            "osx",
            "ios",
        ]
        assert built_order == expected_order

    def test_sdk_versions_forwarded_to_build_image(self):
        # build_and_push threads [build].xcode_sdkv / apple_sdkv down to
        # build_image so the Apple Dockerfiles receive them as --build-arg.
        captured: list[dict] = []

        def fake_build_image(**kwargs):
            captured.append(kwargs)

        with (
            patch(
                "scripts.container_builder.build_image", side_effect=fake_build_image
            ),
            patch("scripts.container_builder.extract_apple_sdks", return_value=0),
        ):
            with patch("scripts.container_builder.is_image_built", return_value=False):
                with patch(
                    "scripts.container_builder.shutil.which",
                    return_value="/usr/bin/docker",
                ):
                    rc = build_and_push(
                        types=["osx"],
                        version=_VERSION,
                        containers_dir=_CONTAINERS_DIR,
                        registry=_REGISTRY,
                        username=_USERNAME,
                        push=False,
                        dry_run=False,
                        xcode_sdkv="26.1.1",
                        apple_sdkv="26.1",
                    )

        assert rc == 0
        osx_call = next(c for c in captured if c["container_type"] == "osx")
        assert osx_call["xcode_sdkv"] == "26.1.1"
        assert osx_call["apple_sdkv"] == "26.1"

    def test_apple_chain_ordered_xcode_then_osx_then_ios(self):
        """osx must build after xcode, and ios after osx (image dependency)."""
        built_order: list[str] = []

        def fake_build_image(container_type, **kwargs):
            built_order.append(container_type)

        with (
            patch(
                "scripts.container_builder.build_image", side_effect=fake_build_image
            ),
            patch("scripts.container_builder.extract_apple_sdks", return_value=0),
        ):
            with patch("scripts.container_builder.is_image_built", return_value=False):
                with patch(
                    "scripts.container_builder.shutil.which",
                    return_value="/usr/bin/docker",
                ):
                    build_and_push(
                        types=["ios", "osx", "xcode"],
                        version=_VERSION,
                        containers_dir=_CONTAINERS_DIR,
                        registry=_REGISTRY,
                        username=_USERNAME,
                        push=False,
                        dry_run=False,
                    )

        assert built_order.index("xcode") < built_order.index("osx")
        assert built_order.index("osx") < built_order.index("ios")


# ---------------------------------------------------------------------------
# TestExtractAppleSdks
# ---------------------------------------------------------------------------


class TestExtractAppleSdks:
    """Cover the SDK-extraction orchestration gap between xcode and osx."""

    def test_runs_docker_run_with_expected_image_and_volume_when_xip_present(
        self, tmp_path
    ):
        # Stage a fake containers/files/ with the xip but no extracted tarballs.
        files_dir = tmp_path / "files"
        files_dir.mkdir()
        (files_dir / "Xcode_26.1.1.xip").write_bytes(b"")

        mock_result = MagicMock()
        mock_result.returncode = 0

        with (
            patch(
                "scripts.container_builder.subprocess.run", return_value=mock_result
            ) as mock_run,
            patch("scripts.container_builder.is_image_built", return_value=True),
        ):
            rc = extract_apple_sdks(
                version=_VERSION,
                containers_dir=tmp_path,
                dry_run=False,
            )

        assert rc == 0
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "docker"
        assert cmd[1] == "run"
        assert "--rm" in cmd
        # -v <files_dir>:/root/files mount.
        assert "-v" in cmd
        volume_idx = cmd.index("-v") + 1
        assert cmd[volume_idx] == f"{files_dir}:/root/files"
        # Final positional arg is the image tag.
        assert cmd[-1] == f"godot-xcode:{_VERSION}"

    def test_no_op_when_sdk_tarballs_already_present(self, tmp_path, caplog):
        import logging

        files_dir = tmp_path / "files"
        files_dir.mkdir()
        (files_dir / "MacOSX26.1.sdk.tar.xz").write_bytes(b"")
        # Note: no xip — extraction should still skip without erroring because
        # the tarballs are the operator-visible proof of work.

        with patch("scripts.container_builder.subprocess.run") as mock_run:
            with caplog.at_level(logging.INFO, logger="scripts.container_builder"):
                rc = extract_apple_sdks(
                    version=_VERSION,
                    containers_dir=tmp_path,
                    dry_run=False,
                )

        assert rc == 0
        mock_run.assert_not_called()
        assert any("already present" in r.message for r in caplog.records)

    def test_hard_errors_when_neither_xip_nor_tarballs_present(self, tmp_path, caplog):
        import logging

        files_dir = tmp_path / "files"
        files_dir.mkdir()
        # files_dir exists but contains nothing — neither xip nor tarballs.

        with patch("scripts.container_builder.subprocess.run") as mock_run:
            with caplog.at_level(logging.ERROR, logger="scripts.container_builder"):
                rc = extract_apple_sdks(
                    version=_VERSION,
                    containers_dir=tmp_path,
                    dry_run=False,
                )

        assert rc == 4
        mock_run.assert_not_called()
        assert any(
            "Xcode_*.xip" in r.message or "pre-extracted" in r.message
            for r in caplog.records
        )

    def test_errors_when_godot_xcode_image_missing_locally(self, tmp_path, caplog):
        import logging

        files_dir = tmp_path / "files"
        files_dir.mkdir()
        (files_dir / "Xcode_26.1.1.xip").write_bytes(b"")

        with (
            patch("scripts.container_builder.is_image_built", return_value=False),
            patch("scripts.container_builder.subprocess.run") as mock_run,
        ):
            with caplog.at_level(logging.ERROR, logger="scripts.container_builder"):
                rc = extract_apple_sdks(
                    version=_VERSION,
                    containers_dir=tmp_path,
                    dry_run=False,
                )

        assert rc == 4
        mock_run.assert_not_called()
        assert any("not found locally" in r.message for r in caplog.records)

    def test_dry_run_logs_without_invoking_docker(self, tmp_path, caplog):
        import logging

        files_dir = tmp_path / "files"
        files_dir.mkdir()
        (files_dir / "Xcode_26.1.1.xip").write_bytes(b"")

        with patch("scripts.container_builder.subprocess.run") as mock_run:
            with caplog.at_level(logging.INFO, logger="scripts.container_builder"):
                rc = extract_apple_sdks(
                    version=_VERSION,
                    containers_dir=tmp_path,
                    dry_run=True,
                )

        assert rc == 0
        mock_run.assert_not_called()
        assert any("dry-run" in r.message for r in caplog.records)

    def test_docker_run_failure_returns_exit_code_4(self, tmp_path):
        import subprocess as _sp

        files_dir = tmp_path / "files"
        files_dir.mkdir()
        (files_dir / "Xcode_26.1.1.xip").write_bytes(b"")

        with (
            patch("scripts.container_builder.is_image_built", return_value=True),
            patch(
                "scripts.container_builder.subprocess.run",
                side_effect=_sp.CalledProcessError(2, "docker"),
            ),
        ):
            rc = extract_apple_sdks(
                version=_VERSION,
                containers_dir=tmp_path,
                dry_run=False,
            )

        assert rc == 4

    def test_force_reruns_extraction_even_when_tarballs_present(self, tmp_path):
        files_dir = tmp_path / "files"
        files_dir.mkdir()
        (files_dir / "MacOSX26.1.sdk.tar.xz").write_bytes(b"")
        (files_dir / "Xcode_26.1.1.xip").write_bytes(b"")

        mock_result = MagicMock()
        mock_result.returncode = 0

        with (
            patch("scripts.container_builder.is_image_built", return_value=True),
            patch(
                "scripts.container_builder.subprocess.run", return_value=mock_result
            ) as mock_run,
        ):
            rc = extract_apple_sdks(
                version=_VERSION,
                containers_dir=tmp_path,
                dry_run=False,
                force=True,
            )

        assert rc == 0
        mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# TestBuildAndPushAppleSdkOrchestration
# ---------------------------------------------------------------------------


class TestBuildAndPushAppleSdkOrchestration:
    """``build_and_push`` must wire ``extract_apple_sdks`` between xcode and osx."""

    def test_extract_apple_sdks_called_once_when_osx_requested(self):
        built_order: list[str] = []
        extract_calls: list[dict] = []

        def fake_build_image(container_type, **kwargs):
            built_order.append(container_type)

        def fake_extract(**kwargs):
            extract_calls.append(kwargs)
            return 0

        with (
            patch(
                "scripts.container_builder.build_image", side_effect=fake_build_image
            ),
            patch(
                "scripts.container_builder.extract_apple_sdks", side_effect=fake_extract
            ),
            patch("scripts.container_builder.is_image_built", return_value=False),
            patch(
                "scripts.container_builder.shutil.which", return_value="/usr/bin/docker"
            ),
        ):
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
        assert len(extract_calls) == 1
        # Must run after xcode and before osx.
        assert "xcode" in built_order and "osx" in built_order
        # build_image is the only thing recorded in built_order — extract_calls
        # capture the moment of extraction. The contract is that extraction
        # happens between xcode's build_image call and osx's build_image call.
        # We check this by re-running with a combined recorder.

    def test_extract_runs_between_xcode_and_osx_in_build_order(self):
        events: list[str] = []

        def fake_build_image(container_type, **kwargs):
            events.append(f"build:{container_type}")

        def fake_extract(**kwargs):
            events.append("extract")
            return 0

        with (
            patch(
                "scripts.container_builder.build_image", side_effect=fake_build_image
            ),
            patch(
                "scripts.container_builder.extract_apple_sdks", side_effect=fake_extract
            ),
            patch("scripts.container_builder.is_image_built", return_value=False),
            patch(
                "scripts.container_builder.shutil.which", return_value="/usr/bin/docker"
            ),
        ):
            build_and_push(
                types=["all"],
                version=_VERSION,
                containers_dir=_CONTAINERS_DIR,
                registry=_REGISTRY,
                username=_USERNAME,
                push=False,
                dry_run=False,
            )

        assert events.index("build:xcode") < events.index("extract")
        assert events.index("extract") < events.index("build:osx")
        # And must run exactly once even though both osx and ios consume it.
        assert events.count("extract") == 1

    def test_extract_not_called_when_no_apple_consumer_types_requested(self):
        extract_calls: list[dict] = []

        def fake_extract(**kwargs):
            extract_calls.append(kwargs)
            return 0

        with (
            patch("scripts.container_builder.build_image"),
            patch(
                "scripts.container_builder.extract_apple_sdks", side_effect=fake_extract
            ),
            patch("scripts.container_builder.is_image_built", return_value=False),
            patch(
                "scripts.container_builder.shutil.which", return_value="/usr/bin/docker"
            ),
        ):
            rc = build_and_push(
                types=["linux", "windows", "web"],
                version=_VERSION,
                containers_dir=_CONTAINERS_DIR,
                registry=_REGISTRY,
                username=_USERNAME,
                push=False,
                dry_run=False,
            )

        assert rc == 0
        assert extract_calls == []

    def test_extract_not_called_when_only_xcode_requested(self):
        """Operators may want to pre-build xcode without extracting yet."""
        extract_calls: list[dict] = []

        def fake_extract(**kwargs):
            extract_calls.append(kwargs)
            return 0

        with (
            patch("scripts.container_builder.build_image"),
            patch(
                "scripts.container_builder.extract_apple_sdks", side_effect=fake_extract
            ),
            patch("scripts.container_builder.is_image_built", return_value=False),
            patch(
                "scripts.container_builder.shutil.which", return_value="/usr/bin/docker"
            ),
        ):
            rc = build_and_push(
                types=["xcode"],
                version=_VERSION,
                containers_dir=_CONTAINERS_DIR,
                registry=_REGISTRY,
                username=_USERNAME,
                push=False,
                dry_run=False,
            )

        assert rc == 0
        assert extract_calls == []

    def test_extract_failure_short_circuits_with_exit_code_4(self):
        def fake_extract(**kwargs):
            return 4

        built: list[str] = []

        def fake_build_image(container_type, **kwargs):
            built.append(container_type)

        with (
            patch(
                "scripts.container_builder.build_image", side_effect=fake_build_image
            ),
            patch(
                "scripts.container_builder.extract_apple_sdks", side_effect=fake_extract
            ),
            patch("scripts.container_builder.is_image_built", return_value=False),
            patch(
                "scripts.container_builder.shutil.which", return_value="/usr/bin/docker"
            ),
        ):
            rc = build_and_push(
                types=["osx"],
                version=_VERSION,
                containers_dir=_CONTAINERS_DIR,
                registry=_REGISTRY,
                username=_USERNAME,
                push=False,
                dry_run=False,
            )

        assert rc == 4
        # osx must NOT have been built after the extraction failure.
        assert "osx" not in built
