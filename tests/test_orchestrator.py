"""Tests for scripts/orchestrator.py — host orchestrator, packager, and gh.

All Docker / subprocess / gh calls are mocked: no real Docker, no real network,
no real gh.
"""

from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from scripts.orchestrator import (
    PublishError,
    build_create_command,
    build_delete_release_asset_command,
    build_list_release_assets_command,
    build_nuget_delete_version_command,
    build_nuget_list_versions_command,
    build_nuget_push_command,
    clear_release_assets,
    collect_nupkgs,
    collect_release_assets,
    default_nuget_source,
    delete_nupkg_versions,
    disambiguate_asset_names,
    dispatch_build,
    nuget_token_from_env,
    package_release,
    publish_nupkgs,
    publish_release,
    read_nupkg_identity,
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
# build_create_command + override helpers
# ---------------------------------------------------------------------------


class TestBuildCreateCommand:
    def test_uses_create_with_prerelease(self):
        cmd = build_create_command(
            tag="v4.7.dev1",
            repo="nongvantinh/godot-build-scripts",
            assets=["/tmp/a.zip"],
            prerelease=True,
            draft=False,
        )

        assert cmd[:3] == ["gh", "release", "create"]
        assert "v4.7.dev1" in cmd
        assert "--prerelease" in cmd
        # `--clobber` is an upload-only flag; the create path must never use it.
        assert "--clobber" not in cmd

    def test_includes_draft_flag_when_draft_requested(self):
        cmd = build_create_command(
            tag="v4.7.dev1",
            repo="r/r",
            assets=["/tmp/a.zip"],
            prerelease=False,
            draft=True,
        )

        assert "--draft" in cmd


class TestDisambiguateAssetNames:
    def test_keeps_unique_basenames(self, tmp_path):
        a = tmp_path / "Godot_linux.zip"
        b = tmp_path / "mono" / "Godot_mono_linux.zip"
        b.parent.mkdir()
        for p in (a, b):
            p.write_text("x")
        names = disambiguate_asset_names([a, b])
        assert names[a] == "Godot_linux.zip"
        assert names[b] == "Godot_mono_linux.zip"

    def test_disambiguates_colliding_subdir_file(self, tmp_path):
        # Classical SHA at root keeps its name; the mono/ one is suffixed.
        root_sha = tmp_path / "SHA512-SUMS.txt"
        mono_sha = tmp_path / "mono" / "SHA512-SUMS.txt"
        mono_sha.parent.mkdir()
        for p in (root_sha, mono_sha):
            p.write_text("x")
        names = disambiguate_asset_names([root_sha, mono_sha])
        assert names[root_sha] == "SHA512-SUMS.txt"
        assert names[mono_sha] == "SHA512-SUMS-mono.txt"

    def test_empty(self):
        assert disambiguate_asset_names([]) == {}


class TestBuildReleaseAssetCommands:
    def test_list_assets_command(self):
        cmd = build_list_release_assets_command(tag="4.7.beta", repo="o/r")
        assert cmd[:4] == ["gh", "release", "view", "4.7.beta"]
        assert cmd[-2:] == ["--jq", ".assets[].name"]

    def test_delete_asset_command(self):
        cmd = build_delete_release_asset_command(
            tag="4.7.beta", repo="o/r", asset_name="x.zip"
        )
        assert cmd == [
            "gh",
            "release",
            "delete-asset",
            "4.7.beta",
            "x.zip",
            "--repo",
            "o/r",
            "--yes",
        ]


class TestClearReleaseAssets:
    def test_dry_run_does_not_invoke_gh(self):
        with mock.patch("scripts.orchestrator.subprocess.run") as run:
            clear_release_assets(tag="4.7.beta", repo="o/r", dry_run=True)
        run.assert_not_called()

    def test_noop_when_release_missing(self):
        missing = subprocess.CompletedProcess([], 1, stdout="", stderr="not found")
        with mock.patch(
            "scripts.orchestrator.subprocess.run", side_effect=[missing]
        ) as run:
            clear_release_assets(tag="4.7.beta", repo="o/r", dry_run=False)
        assert run.call_count == 1  # only the list call; nothing to delete

    def test_deletes_each_existing_asset(self):
        listing = subprocess.CompletedProcess([], 0, stdout="a.zip\nb.zip\n", stderr="")
        ok = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch(
            "scripts.orchestrator.subprocess.run", side_effect=[listing, ok, ok]
        ) as run:
            clear_release_assets(tag="4.7.beta", repo="o/r", dry_run=False)
        assert run.call_count == 3
        assert run.call_args_list[1].args[0][:3] == ["gh", "release", "delete-asset"]

    def test_raises_when_delete_fails(self):
        listing = subprocess.CompletedProcess([], 0, stdout="a.zip\n", stderr="")
        fail = subprocess.CompletedProcess([], 1, stdout="", stderr="boom")
        with mock.patch(
            "scripts.orchestrator.subprocess.run", side_effect=[listing, fail]
        ):
            with pytest.raises(PublishError, match="Failed to delete existing asset"):
                clear_release_assets(tag="4.7.beta", repo="o/r", dry_run=False)


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

    def test_clears_then_uploads_when_release_exists(self, tmp_path):
        # Override path: existing assets are cleared, then a plain upload (no
        # --clobber) lands the fresh assets on the clean slate.
        asset = tmp_path / "a.zip"
        asset.write_text("x")
        with (
            mock.patch("scripts.orchestrator._gh_available", return_value=True),
            mock.patch("scripts.orchestrator._release_exists", return_value=True),
            mock.patch("scripts.orchestrator.clear_release_assets") as clear,
            mock.patch("scripts.orchestrator.subprocess.run") as run,
        ):
            run.return_value = subprocess.CompletedProcess([], 0)
            publish_release(
                tag="4.7.beta",
                repo="r/r",
                assets=[asset],
                prerelease=True,
                draft=False,
                dry_run=False,
            )

        clear.assert_called_once()
        cmd = run.call_args[0][0]
        assert cmd[:3] == ["gh", "release", "upload"]
        assert "--clobber" not in cmd


# ---------------------------------------------------------------------------
# NuGet publishing
# ---------------------------------------------------------------------------


def _make_nupkgs(out_dir: Path, plat: str, arch: str, names: list[str]) -> None:
    """Create dummy ``.nupkg`` files under a platform/arch tools-mono tree."""
    nupkgs = out_dir / plat / arch / "tools-mono" / "GodotSharp" / "Tools" / "nupkgs"
    nupkgs.mkdir(parents=True, exist_ok=True)
    for name in names:
        (nupkgs / name).write_text("x")


class TestDefaultNugetSource:
    def test_derives_github_packages_feed_from_username(self):
        assert (
            default_nuget_source("nongvantinh")
            == "https://nuget.pkg.github.com/nongvantinh/index.json"
        )


class TestNugetTokenFromEnv:
    def test_prefers_github_personal_access_token(self, monkeypatch):
        monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "from-pat")
        monkeypatch.setenv("GHCR_PAT", "from-ghcr")
        monkeypatch.setenv("GITHUB_TOKEN", "from-gh")
        assert nuget_token_from_env() == "from-pat"

    def test_falls_back_to_ghcr_pat(self, monkeypatch):
        monkeypatch.delenv("GITHUB_PERSONAL_ACCESS_TOKEN", raising=False)
        monkeypatch.setenv("GHCR_PAT", "from-ghcr")
        monkeypatch.setenv("GITHUB_TOKEN", "from-gh")
        assert nuget_token_from_env() == "from-ghcr"

    def test_falls_back_to_github_token(self, monkeypatch):
        monkeypatch.delenv("GITHUB_PERSONAL_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("GHCR_PAT", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "from-gh")
        assert nuget_token_from_env() == "from-gh"

    def test_returns_none_when_unset(self, monkeypatch):
        monkeypatch.delenv("GITHUB_PERSONAL_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("GHCR_PAT", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        assert nuget_token_from_env() is None


class TestCollectNupkgs:
    def test_returns_empty_when_out_dir_missing(self, tmp_path):
        assert collect_nupkgs(tmp_path / "nope") == []

    def test_returns_empty_for_classical_only_build(self, tmp_path):
        # No tools-mono tree at all (classical build).
        (tmp_path / "linux" / "x86_64" / "tools").mkdir(parents=True)
        assert collect_nupkgs(tmp_path) == []

    def test_deduplicates_by_filename_across_platforms(self, tmp_path):
        pkgs = ["GodotSharp.4.7.0-beta.nupkg", "Godot.NET.Sdk.4.7.0-beta.nupkg"]
        for plat, arch in (("linux", "x86_64"), ("windows", "x86_64"), ("macos", "")):
            _make_nupkgs(tmp_path, plat, arch or "tools-host", pkgs)

        result = collect_nupkgs(tmp_path)

        names = sorted(p.name for p in result)
        assert names == sorted(pkgs)  # one copy of each, no duplicates

    def test_excludes_snupkg_symbol_packages(self, tmp_path):
        _make_nupkgs(
            tmp_path,
            "linux",
            "x86_64",
            ["GodotSharp.4.7.0-beta.nupkg", "GodotSharp.4.7.0-beta.snupkg"],
        )
        result = collect_nupkgs(tmp_path)
        assert [p.name for p in result] == ["GodotSharp.4.7.0-beta.nupkg"]

    def test_prefers_linux_x86_64_canonical_source(self, tmp_path):
        # windows created first (alphabetically earlier in some orders); the
        # linux/x86_64 copy must still win as the canonical source.
        _make_nupkgs(tmp_path, "windows", "arm64", ["GodotSharp.4.7.0-beta.nupkg"])
        _make_nupkgs(tmp_path, "linux", "x86_64", ["GodotSharp.4.7.0-beta.nupkg"])

        result = collect_nupkgs(tmp_path)

        assert len(result) == 1
        parts = set(result[0].parts)
        assert "linux" in parts and "x86_64" in parts


class TestBuildNugetPushCommand:
    def test_includes_source_apikey_skipdup_and_nosymbols(self):
        cmd = build_nuget_push_command(
            nupkg="/tmp/GodotSharp.nupkg",
            source="https://nuget.pkg.github.com/nongvantinh/index.json",
            api_key="secret",
        )
        assert cmd[:4] == ["dotnet", "nuget", "push", "/tmp/GodotSharp.nupkg"]
        assert "--skip-duplicate" in cmd
        assert "--no-symbols" in cmd
        assert cmd[cmd.index("--source") + 1].endswith("/nongvantinh/index.json")
        assert cmd[cmd.index("--api-key") + 1] == "secret"


class TestPublishNupkgs:
    def test_noop_when_no_nupkgs(self):
        with mock.patch("scripts.orchestrator.subprocess.run") as run:
            publish_nupkgs(nupkgs=[], source="s", api_key="k", dry_run=False)
        run.assert_not_called()

    def test_dry_run_does_not_invoke_dotnet(self, tmp_path):
        pkg = tmp_path / "GodotSharp.nupkg"
        pkg.write_text("x")
        with mock.patch("scripts.orchestrator.subprocess.run") as run:
            publish_nupkgs(nupkgs=[pkg], source="s", api_key="", dry_run=True)
        run.assert_not_called()

    def test_raises_when_dotnet_unavailable(self, tmp_path):
        pkg = tmp_path / "GodotSharp.nupkg"
        pkg.write_text("x")
        with mock.patch("scripts.orchestrator._dotnet_available", return_value=False):
            with pytest.raises(PublishError, match="dotnet CLI not found"):
                publish_nupkgs(nupkgs=[pkg], source="s", api_key="k", dry_run=False)

    def test_raises_when_token_missing(self, tmp_path):
        pkg = tmp_path / "GodotSharp.nupkg"
        pkg.write_text("x")
        with mock.patch("scripts.orchestrator._dotnet_available", return_value=True):
            with pytest.raises(PublishError, match="No GitHub Packages token"):
                publish_nupkgs(nupkgs=[pkg], source="s", api_key="", dry_run=False)

    def test_pushes_each_package(self, tmp_path):
        pkgs = [tmp_path / "A.nupkg", tmp_path / "B.nupkg"]
        for p in pkgs:
            p.write_text("x")
        with (
            mock.patch("scripts.orchestrator._dotnet_available", return_value=True),
            mock.patch("scripts.orchestrator.subprocess.run") as run,
        ):
            run.return_value = subprocess.CompletedProcess([], 0)
            publish_nupkgs(nupkgs=pkgs, source="s", api_key="k", dry_run=False)
        assert run.call_count == 2

    def test_raises_publish_error_when_push_exits_nonzero(self, tmp_path):
        pkg = tmp_path / "GodotSharp.nupkg"
        pkg.write_text("x")
        with (
            mock.patch("scripts.orchestrator._dotnet_available", return_value=True),
            mock.patch(
                "scripts.orchestrator.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "dotnet"),
            ),
        ):
            with pytest.raises(PublishError, match="dotnet nuget push"):
                publish_nupkgs(nupkgs=[pkg], source="s", api_key="k", dry_run=False)


# ---------------------------------------------------------------------------
# NuGet overwrite (delete-before-push)
# ---------------------------------------------------------------------------


def _make_real_nupkg(path: Path, pkg_id: str, version: str) -> Path:
    """Write a minimal valid ``.nupkg`` (zip with one ``.nuspec``) to *path*."""
    nuspec = (
        '<?xml version="1.0"?>'
        '<package xmlns="http://schemas.microsoft.com/packaging/2013/05/nuspec.xsd">'
        f"<metadata><id>{pkg_id}</id><version>{version}</version>"
        f"<authors>Godot</authors><description>d</description></metadata>"
        "</package>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{pkg_id}.nuspec", nuspec)
    return path


class TestReadNupkgIdentity:
    def test_reads_id_and_version_from_nuspec(self, tmp_path):
        pkg = _make_real_nupkg(
            tmp_path / "Godot.NET.Sdk.4.7.0-beta.nupkg", "Godot.NET.Sdk", "4.7.0-beta"
        )
        assert read_nupkg_identity(pkg) == ("Godot.NET.Sdk", "4.7.0-beta")

    def test_raises_when_no_nuspec(self, tmp_path):
        pkg = tmp_path / "broken.nupkg"
        with zipfile.ZipFile(pkg, "w") as archive:
            archive.writestr("readme.txt", "no nuspec here")
        with pytest.raises(PublishError, match="No .nuspec"):
            read_nupkg_identity(pkg)


class TestBuildNugetVersionCommands:
    def test_list_versions_url_encodes_package_name(self):
        cmd = build_nuget_list_versions_command(
            username="nongvantinh", package_name="Godot.NET.Sdk"
        )
        assert cmd[:3] == ["gh", "api", "--paginate"]
        assert cmd[-1] == (
            "/users/nongvantinh/packages/nuget/Godot.NET.Sdk/versions"
        )

    def test_delete_version_targets_version_id(self):
        cmd = build_nuget_delete_version_command(
            username="nongvantinh", package_name="GodotSharp", version_id=42
        )
        assert cmd[:4] == ["gh", "api", "-X", "DELETE"]
        assert cmd[-1] == (
            "/users/nongvantinh/packages/nuget/GodotSharp/versions/42"
        )


class TestDeleteNupkgVersions:
    def test_noop_when_no_nupkgs(self):
        with mock.patch("scripts.orchestrator.subprocess.run") as run:
            delete_nupkg_versions(
                nupkgs=[], username="nongvantinh", api_key="k", dry_run=False
            )
        run.assert_not_called()

    def test_dry_run_does_not_invoke_gh(self, tmp_path):
        pkg = _make_real_nupkg(
            tmp_path / "GodotSharp.4.7.0-beta.nupkg", "GodotSharp", "4.7.0-beta"
        )
        with mock.patch("scripts.orchestrator.subprocess.run") as run:
            delete_nupkg_versions(
                nupkgs=[pkg], username="nongvantinh", api_key="k", dry_run=True
            )
        run.assert_not_called()

    def test_raises_when_gh_unavailable(self, tmp_path):
        pkg = _make_real_nupkg(
            tmp_path / "GodotSharp.4.7.0-beta.nupkg", "GodotSharp", "4.7.0-beta"
        )
        with mock.patch("scripts.orchestrator._gh_available", return_value=False):
            with pytest.raises(PublishError, match="gh CLI not found"):
                delete_nupkg_versions(
                    nupkgs=[pkg], username="nongvantinh", api_key="k", dry_run=False
                )

    def test_raises_when_api_key_missing(self, tmp_path):
        pkg = _make_real_nupkg(
            tmp_path / "GodotSharp.4.7.0-beta.nupkg", "GodotSharp", "4.7.0-beta"
        )
        with mock.patch("scripts.orchestrator._gh_available", return_value=True):
            with pytest.raises(PublishError, match="No GitHub Packages token"):
                delete_nupkg_versions(
                    nupkgs=[pkg], username="nongvantinh", api_key="", dry_run=False
                )

    def test_deletes_matching_version(self, tmp_path):
        pkg = _make_real_nupkg(
            tmp_path / "GodotSharp.4.7.0-beta.nupkg", "GodotSharp", "4.7.0-beta"
        )
        listing = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps([{"id": 7, "name": "4.7.0-beta"}, {"id": 1, "name": "4.4.1-stable-.1"}]), stderr=""
        )
        deletion = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with (
            mock.patch("scripts.orchestrator._gh_available", return_value=True),
            mock.patch(
                "scripts.orchestrator.subprocess.run",
                side_effect=[listing, deletion],
            ) as run,
        ):
            delete_nupkg_versions(
                nupkgs=[pkg], username="nongvantinh", api_key="k", dry_run=False
            )
        # Second call is the DELETE of version id 7.
        delete_cmd = run.call_args_list[1].args[0]
        assert delete_cmd[:4] == ["gh", "api", "-X", "DELETE"]
        assert delete_cmd[-1].endswith("/GodotSharp/versions/7")

    def test_noop_when_version_absent_from_listing(self, tmp_path):
        pkg = _make_real_nupkg(
            tmp_path / "GodotSharp.4.7.0-beta.nupkg", "GodotSharp", "4.7.0-beta"
        )
        listing = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps([{"id": 1, "name": "4.4.1-stable-.1"}]), stderr=""
        )
        with (
            mock.patch("scripts.orchestrator._gh_available", return_value=True),
            mock.patch(
                "scripts.orchestrator.subprocess.run", side_effect=[listing]
            ) as run,
        ):
            delete_nupkg_versions(
                nupkgs=[pkg], username="nongvantinh", api_key="k", dry_run=False
            )
        # Only the list call ran; no DELETE.
        assert run.call_count == 1

    def test_noop_when_package_not_published(self, tmp_path):
        pkg = _make_real_nupkg(
            tmp_path / "GodotSharp.4.7.0-beta.nupkg", "GodotSharp", "4.7.0-beta"
        )
        not_found = subprocess.CompletedProcess(
            [], 1, stdout="", stderr="gh: Not Found (HTTP 404)"
        )
        with (
            mock.patch("scripts.orchestrator._gh_available", return_value=True),
            mock.patch(
                "scripts.orchestrator.subprocess.run", side_effect=[not_found]
            ) as run,
        ):
            delete_nupkg_versions(
                nupkgs=[pkg], username="nongvantinh", api_key="k", dry_run=False
            )
        assert run.call_count == 1

    def test_raises_when_list_fails_hard(self, tmp_path):
        pkg = _make_real_nupkg(
            tmp_path / "GodotSharp.4.7.0-beta.nupkg", "GodotSharp", "4.7.0-beta"
        )
        boom = subprocess.CompletedProcess(
            [], 1, stdout="", stderr="gh: Bad credentials (HTTP 401)"
        )
        with (
            mock.patch("scripts.orchestrator._gh_available", return_value=True),
            mock.patch("scripts.orchestrator.subprocess.run", side_effect=[boom]),
        ):
            with pytest.raises(PublishError, match="Failed to list NuGet versions"):
                delete_nupkg_versions(
                    nupkgs=[pkg], username="nongvantinh", api_key="k", dry_run=False
                )

    def test_raises_when_delete_fails(self, tmp_path):
        pkg = _make_real_nupkg(
            tmp_path / "GodotSharp.4.7.0-beta.nupkg", "GodotSharp", "4.7.0-beta"
        )
        listing = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps([{"id": 7, "name": "4.7.0-beta"}]), stderr=""
        )
        delete_fail = subprocess.CompletedProcess(
            [], 1, stdout="", stderr="gh: Forbidden (HTTP 403)"
        )
        with (
            mock.patch("scripts.orchestrator._gh_available", return_value=True),
            mock.patch(
                "scripts.orchestrator.subprocess.run",
                side_effect=[listing, delete_fail],
            ),
        ):
            with pytest.raises(PublishError, match="Failed to delete NuGet"):
                delete_nupkg_versions(
                    nupkgs=[pkg], username="nongvantinh", api_key="k", dry_run=False
                )
