"""Tests for scripts/orchestrator.py — host orchestrator, packager, and gh.

All Docker / subprocess / gh calls are mocked: no real Docker, no real network,
no real gh.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from scripts.orchestrator import (
    PublishError,
    build_publish_command,
    collect_release_assets,
    dispatch_build,
    package_release,
    publish_release,
)

# ---------------------------------------------------------------------------
# dispatch_build — delegates to scripts.host_orchestrator.run_build
# ---------------------------------------------------------------------------


class TestDispatchBuild:
    """``dispatch_build`` delegates to :func:`scripts.host_orchestrator.run_build`."""

    def test_dry_run_threads_through_to_host_orchestrator(self):
        with mock.patch("scripts.orchestrator.host_orchestrator.run_build") as run:
            run.return_value = 0
            rc = dispatch_build(
                build_dir=Path("/tmp/bd"),
                build_type="all",
                num_cores=10,
                git_branch="4.7.dev1",
                godot_repo="nongvantinh/godot",
                registry="ghcr.io",
                username="nongvantinh",
                container_version="4.7",
                dry_run=True,
            )

        assert rc == 0
        run.assert_called_once()
        assert run.call_args.kwargs["dry_run"] is True

    def test_passes_build_type_and_num_cores(self):
        with mock.patch("scripts.orchestrator.host_orchestrator.run_build") as run:
            run.return_value = 0
            dispatch_build(
                build_dir=Path("/tmp/bd"),
                build_type="mono",
                num_cores=8,
                git_branch="4.7.dev1",
                godot_repo="nongvantinh/godot",
                registry="ghcr.io",
                username="nongvantinh",
                container_version="4.7",
                dry_run=False,
            )

        kwargs = run.call_args.kwargs
        assert kwargs["build_type"] == "mono"
        assert kwargs["num_cores"] == 8

    def test_passes_container_version_to_host_orchestrator(self):
        # The image-tag scheme is godot-<plat>:<container_version>; the host
        # orchestrator resolves tags before extracting the engine version, so
        # the orchestrator must hand it container_version=<godot_version>
        # (e.g. "4.7") to match config.toml and `containers --push`.
        with mock.patch("scripts.orchestrator.host_orchestrator.run_build") as run:
            run.return_value = 0
            dispatch_build(
                build_dir=Path("/tmp/bd"),
                build_type="all",
                num_cores=8,
                git_branch="4.7.dev1",
                godot_repo="nongvantinh/godot",
                registry="ghcr.io",
                username="nongvantinh",
                container_version="4.7",
                dry_run=False,
            )

        assert run.call_args.kwargs["container_version"] == "4.7"

    def test_apple_gate_kwarg_is_not_threaded_to_host_orchestrator(self):
        # Apple targets always attempt to build. The host orchestrator no
        # longer accepts an apple-gate kwarg; dispatch_build must not pass one.
        with mock.patch("scripts.orchestrator.host_orchestrator.run_build") as run:
            run.return_value = 0
            dispatch_build(
                build_dir=Path("/tmp/bd"),
                build_type="all",
                num_cores=10,
                git_branch="4.7.dev1",
                godot_repo="nongvantinh/godot",
                registry="ghcr.io",
                username="nongvantinh",
                container_version="4.7",
                dry_run=False,
            )

        apple_gate_kwargs = [k for k in run.call_args.kwargs if "apple" in k.lower()]
        assert apple_gate_kwargs == []

    def test_resolves_upstream_godot_dir_from_build_dir_parent(self, tmp_path):
        # The host orchestrator needs the upstream/godot submodule path; the
        # orchestrator computes it relative to build_dir.parent so the wiring
        # stays consistent with the rest of build-godot.py.
        with mock.patch("scripts.orchestrator.host_orchestrator.run_build") as run:
            run.return_value = 0
            dispatch_build(
                build_dir=tmp_path / "build-godot-and-templates",
                build_type="all",
                num_cores=10,
                git_branch="4.7.dev1",
                godot_repo="nongvantinh/godot",
                registry="ghcr.io",
                username="nongvantinh",
                container_version="4.7",
                dry_run=True,
            )

        expected = (tmp_path / "upstream" / "godot").resolve()
        assert run.call_args.kwargs["upstream_godot_dir"] == expected


# ---------------------------------------------------------------------------
# package_release
# ---------------------------------------------------------------------------


class TestPackageRelease:
    def test_delegates_to_python_packager_with_version_args(self):
        # ``package_release`` calls :func:`scripts.packager.package_release`
        # in-process — never subprocess.
        with mock.patch("scripts.orchestrator.packager.package_release") as pr:
            pr.return_value = 0
            rc = package_release(
                build_dir=Path("/tmp/bd"),
                godot_version="4.7",
                godot_version_status="dev1",
                dry_run=False,
            )

        assert rc == 0
        pr.assert_called_once()
        kwargs = pr.call_args.kwargs
        assert kwargs["basedir"] == Path("/tmp/bd")
        assert kwargs["godot_version"] == "4.7"
        assert kwargs["godot_version_status"] == "dev1"
        assert kwargs["dry_run"] is False

    def test_dry_run_does_not_call_packager(self):
        with mock.patch("scripts.orchestrator.packager.package_release") as pr:
            rc = package_release(
                build_dir=Path("/tmp/bd"),
                godot_version="4.7",
                godot_version_status="dev1",
                dry_run=True,
            )

        assert rc == 0
        pr.assert_not_called()

    def test_passes_upstream_godot_dir_override(self, tmp_path):
        with mock.patch("scripts.orchestrator.packager.package_release") as pr:
            pr.return_value = 0
            package_release(
                build_dir=Path("/tmp/bd"),
                godot_version="4.7",
                godot_version_status="dev1",
                upstream_godot_dir=tmp_path / "godot",
                dry_run=False,
            )

        assert pr.call_args.kwargs["upstream_godot_dir"] == tmp_path / "godot"


# ---------------------------------------------------------------------------
# collect_release_assets
# ---------------------------------------------------------------------------


class TestCollectReleaseAssets:
    def test_returns_empty_when_dir_missing(self, tmp_path):
        assert collect_release_assets(tmp_path / "nope") == []

    def test_collects_files_including_mono_subdir(self, tmp_path):
        (tmp_path / "Godot_v4.7.dev1_linux.x86_64.zip").write_text("x")
        (tmp_path / "SHA512-SUMS.txt").write_text("x")
        mono = tmp_path / "mono"
        mono.mkdir()
        (mono / "Godot_v4.7.dev1_mono_export_templates.tpz").write_text("x")

        assets = collect_release_assets(tmp_path)

        names = {p.name for p in assets}
        assert "Godot_v4.7.dev1_linux.x86_64.zip" in names
        assert "Godot_v4.7.dev1_mono_export_templates.tpz" in names
        assert "SHA512-SUMS.txt" in names


# ---------------------------------------------------------------------------
# build_publish_command
# ---------------------------------------------------------------------------


class TestBuildPublishCommand:
    def test_uses_create_with_prerelease_when_release_does_not_exist(self):
        cmd = build_publish_command(
            tag="v4.7.dev1",
            repo="nongvantinh/godot-build-scripts",
            assets=["/tmp/a.zip"],
            prerelease=True,
            draft=False,
            release_exists=False,
        )

        assert cmd[:3] == ["gh", "release", "create"]
        assert "v4.7.dev1" in cmd
        assert "--prerelease" in cmd
        # `--clobber` is an `upload`-only flag; `gh release create` rejects it.
        assert "--clobber" not in cmd

    def test_uses_upload_with_clobber_when_release_exists(self):
        cmd = build_publish_command(
            tag="v4.7.dev1",
            repo="nongvantinh/godot-build-scripts",
            assets=["/tmp/a.zip"],
            prerelease=True,
            draft=False,
            release_exists=True,
        )

        assert cmd[:3] == ["gh", "release", "upload"]
        assert "--clobber" in cmd
        assert "--prerelease" not in cmd

    def test_includes_draft_flag_when_draft_requested(self):
        cmd = build_publish_command(
            tag="v4.7.dev1",
            repo="r/r",
            assets=["/tmp/a.zip"],
            prerelease=False,
            draft=True,
            release_exists=False,
        )

        assert "--draft" in cmd


# ---------------------------------------------------------------------------
# publish_release
# ---------------------------------------------------------------------------


class TestPublishRelease:
    def test_dry_run_does_not_invoke_gh(self, tmp_path):
        asset = tmp_path / "a.zip"
        asset.write_text("x")
        with mock.patch("scripts.orchestrator.subprocess.run") as run:
            publish_release(
                tag="v4.7.dev1",
                repo="r/r",
                assets=[asset],
                prerelease=True,
                draft=False,
                dry_run=True,
            )

        run.assert_not_called()

    def test_raises_publish_error_when_no_assets(self):
        with mock.patch("scripts.orchestrator._gh_available", return_value=True):
            with pytest.raises(PublishError, match="No release assets"):
                publish_release(
                    tag="v4.7.dev1",
                    repo="r/r",
                    assets=[],
                    prerelease=True,
                    draft=False,
                    dry_run=False,
                )

    def test_raises_publish_error_when_gh_unavailable(self, tmp_path):
        asset = tmp_path / "a.zip"
        asset.write_text("x")
        with mock.patch("scripts.orchestrator._gh_available", return_value=False):
            with pytest.raises(PublishError, match="gh CLI not found"):
                publish_release(
                    tag="v4.7.dev1",
                    repo="r/r",
                    assets=[asset],
                    prerelease=True,
                    draft=False,
                    dry_run=False,
                )

    def test_raises_publish_error_when_gh_exits_nonzero(self, tmp_path):
        asset = tmp_path / "a.zip"
        asset.write_text("x")
        with (
            mock.patch("scripts.orchestrator._gh_available", return_value=True),
            mock.patch("scripts.orchestrator._release_exists", return_value=False),
            mock.patch(
                "scripts.orchestrator.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "gh"),
            ),
        ):
            with pytest.raises(PublishError, match="gh release"):
                publish_release(
                    tag="v4.7.dev1",
                    repo="r/r",
                    assets=[asset],
                    prerelease=True,
                    draft=False,
                    dry_run=False,
                )

    def test_invokes_create_when_release_absent(self, tmp_path):
        asset = tmp_path / "a.zip"
        asset.write_text("x")
        with (
            mock.patch("scripts.orchestrator._gh_available", return_value=True),
            mock.patch("scripts.orchestrator._release_exists", return_value=False),
            mock.patch("scripts.orchestrator.subprocess.run") as run,
        ):
            run.return_value = subprocess.CompletedProcess([], 0)
            publish_release(
                tag="v4.7.dev1",
                repo="r/r",
                assets=[asset],
                prerelease=True,
                draft=False,
                dry_run=False,
            )

        cmd = run.call_args[0][0]
        assert cmd[:3] == ["gh", "release", "create"]
