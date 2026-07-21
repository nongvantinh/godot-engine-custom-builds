"""Tests for build-godot.py internal helpers and the release exit-code path.

These cover behaviour that the subprocess-driven ``test_cli.py`` cannot assert
cheaply: the CSV/`all`/`both` selector expansion, that Apple platforms always
attempt to build (no host-side skip/auto-detection), and the exit-code-5
release-publish-failure path. All Docker / subprocess / ``gh`` collaborators
are mocked — no real external calls.

``build-godot.py`` is a hyphenated script (not a regular importable module), so
it is loaded once via importlib. The version guard and argparse only fire under
``__main__``, so importing it has no side effects.
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
from unittest import mock

import pytest

_SCRIPT = Path(__file__).parent.parent / "build-godot.py"

_MINIMAL_VALID_TOML = """\
registry = "ghcr.io"
username = "testuser"
godot_version = "4.3"

[[platforms]]
name = "linux"
image = "ghcr.io/test/linux:4.3"
scons_flags = "platform=linuxbsd"
"""


def _load_cli_module():
    spec = importlib.util.spec_from_file_location("build_godot_cli", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def cli():
    return _load_cli_module()


@pytest.fixture()
def config_path(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(_MINIMAL_VALID_TOML, encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# --platform parsing (incl. 'all')
# ---------------------------------------------------------------------------


class TestParsePlatforms:
    def test_all_expands_to_every_supported_platform_when_requested(self, cli):
        result = cli._parse_platforms("all")

        assert set(result) == {"linux", "windows", "macos", "android", "web", "ios"}

    def test_all_leads_with_linux_then_windows_for_desktop_first_build_order(self, cli):
        result = cli._parse_platforms("all")

        assert result[0] == "linux"
        assert result[1] == "windows"

    def test_csv_is_split_and_lowercased_when_multiple_platforms_given(self, cli):
        assert cli._parse_platforms("Linux, Windows") == ["linux", "windows"]

    def test_empty_string_yields_no_platforms(self, cli):
        assert cli._parse_platforms("") == []


# ---------------------------------------------------------------------------
# --mono selector
# ---------------------------------------------------------------------------


class TestResolveMonoVariants:
    def test_both_expands_to_on_and_off_when_requested(self, cli):
        assert cli._resolve_mono_variants("both", ["on"]) == ["on", "off"]

    def test_falls_back_to_config_default_when_not_supplied(self, cli):
        assert cli._resolve_mono_variants(None, ["on", "off"]) == ["on", "off"]

    def test_single_value_yields_single_variant_when_requested(self, cli):
        assert cli._resolve_mono_variants("on", ["on", "off"]) == ["on"]


# ---------------------------------------------------------------------------
# --arch / --flavor / --kind CSV resolution
# ---------------------------------------------------------------------------


class TestResolveCsv:
    def test_falls_back_to_default_when_value_is_none(self, cli):
        assert cli._resolve_csv(None, ["release"]) == ["release"]

    def test_splits_and_strips_whitespace_when_value_supplied(self, cli):
        assert cli._resolve_csv("x86_64, arm64 ,arm32", ["ignored"]) == [
            "x86_64",
            "arm64",
            "arm32",
        ]

    def test_drops_empty_tokens_when_trailing_comma_present(self, cli):
        assert cli._resolve_csv("x86_64,", ["ignored"]) == ["x86_64"]


# ---------------------------------------------------------------------------
# Apple platforms always build — no host-side skip/auto-detection
# ---------------------------------------------------------------------------


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


def _build_args(config_path, platform="macos", arch="universal", **overrides):
    base = dict(
        config=config_path,
        verbose=False,
        platform=platform,
        flavor="release",
        kind="editor",
        mono="off",
        arch=arch,
        godot_repo="official",
        target=None,
        dry_run=True,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class TestAppleAlwaysBuilds:
    """Apple targets always attempt to build — no host-side skip, no auto-detect."""

    def _write_apple_cfg(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text(
            _MINIMAL_VALID_TOML + _APPLE_PLATFORMS_TOML,
            encoding="utf-8",
        )
        return str(path)

    def test_macos_build_proceeds_unconditionally_to_docker(self, cli, tmp_path):
        cfg = self._write_apple_cfg(tmp_path)
        args = _build_args(cfg, platform="macos", arch="universal")

        with (
            mock.patch.object(cli, "apply_patches"),
            mock.patch.object(
                cli,
                "ensure_docker",
                side_effect=cli.DockerUnavailableError("no docker"),
            ),
        ):
            rc = cli.cmd_build(args)

        # Docker required for macOS; ensure_docker is invoked and the build
        # surfaces the unavailability as exit 3 — never as a silent skip.
        assert rc == 3

    def test_ios_build_proceeds_unconditionally_to_docker(self, cli, tmp_path):
        cfg = self._write_apple_cfg(tmp_path)
        args = _build_args(cfg, platform="ios", arch="arm64")

        with (
            mock.patch.object(cli, "apply_patches"),
            mock.patch.object(
                cli,
                "ensure_docker",
                side_effect=cli.DockerUnavailableError("no docker"),
            ),
        ):
            rc = cli.cmd_build(args)

        assert rc == 3


# ---------------------------------------------------------------------------
# release sub-command exit codes (in-process, mocked collaborators)
# ---------------------------------------------------------------------------


def _release_args(config_path, **overrides) -> argparse.Namespace:
    base = dict(
        config=config_path,
        platform="all",
        do_build=False,
        do_package=False,
        do_upload=True,
        do_nuget=False,
        do_nuget_overwrite=None,
        tag=None,
        jobs=10,
        godot_repo=None,
        verbose=False,
        dry_run=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class TestReleaseExitCodes:
    def test_exits_five_when_publish_step_fails(self, cli, config_path):
        args = _release_args(config_path)

        with (
            mock.patch.object(
                cli, "collect_release_assets", return_value=[Path("/tmp/a.zip")]
            ),
            mock.patch.object(
                cli, "publish_release", side_effect=cli.PublishError("boom")
            ),
        ):
            rc = cli.cmd_release(args)

        assert rc == 5

    def test_exits_four_when_build_step_fails(self, cli, config_path):
        args = _release_args(config_path, do_build=True)

        with mock.patch.object(cli, "dispatch_build", return_value=2):
            rc = cli.cmd_release(args)

        assert rc == 4

    def test_exits_four_when_package_step_fails(self, cli, config_path):
        args = _release_args(config_path, do_package=True)

        with mock.patch.object(cli, "package_release", return_value=3):
            rc = cli.cmd_release(args)

        assert rc == 4

    def test_exits_one_when_config_missing(self, cli, tmp_path):
        args = _release_args(str(tmp_path / "nope.toml"))

        rc = cli.cmd_release(args)

        assert rc == 1

    def test_no_upload_returns_zero_without_publishing(self, cli, config_path):
        args = _release_args(config_path, do_upload=False)

        with mock.patch.object(cli, "publish_release") as publish:
            rc = cli.cmd_release(args)

        assert rc == 0
        publish.assert_not_called()

    def test_publishes_when_assets_present_and_upload_enabled(self, cli, config_path):
        args = _release_args(config_path)

        with (
            mock.patch.object(
                cli, "collect_release_assets", return_value=[Path("/tmp/a.zip")]
            ),
            mock.patch.object(cli, "publish_release") as publish,
        ):
            rc = cli.cmd_release(args)

        assert rc == 0
        publish.assert_called_once()

    def test_nuget_published_after_release_upload(self, cli, config_path):
        # do_nuget=True wires collect_nupkgs -> publish_nupkgs after the
        # gh-release upload, deriving the feed from username.
        args = _release_args(config_path, do_nuget=True)

        with (
            mock.patch.object(
                cli, "collect_release_assets", return_value=[Path("/tmp/a.zip")]
            ),
            mock.patch.object(cli, "publish_release"),
            mock.patch.object(
                cli, "collect_nupkgs", return_value=[Path("/tmp/GodotSharp.nupkg")]
            ),
            mock.patch.object(cli, "nuget_token_from_env", return_value="tok"),
            mock.patch.object(cli, "delete_nupkg_versions") as overwrite,
            mock.patch.object(cli, "publish_nupkgs") as push,
        ):
            rc = cli.cmd_release(args)

        assert rc == 0
        push.assert_called_once()
        kwargs = push.call_args.kwargs
        # Feed is derived from the config's username.
        assert kwargs["source"].startswith("https://nuget.pkg.github.com/")
        assert kwargs["source"].endswith("/index.json")
        assert kwargs["api_key"] == "tok"
        # Overwrite is on by default: existing versions are deleted before push.
        overwrite.assert_called_once()
        assert overwrite.call_args.kwargs["api_key"] == "tok"

    def test_nuget_publishes_even_when_release_upload_skipped(self, cli, config_path):
        # NuGet push is an independent publish target: --no-upload + --nuget
        # still pushes packages.
        args = _release_args(config_path, do_upload=False, do_nuget=True)

        with (
            mock.patch.object(
                cli, "collect_nupkgs", return_value=[Path("/tmp/GodotSharp.nupkg")]
            ),
            mock.patch.object(cli, "nuget_token_from_env", return_value="tok"),
            mock.patch.object(cli, "delete_nupkg_versions"),
            mock.patch.object(cli, "publish_nupkgs") as push,
            mock.patch.object(cli, "publish_release") as release,
        ):
            rc = cli.cmd_release(args)

        assert rc == 0
        release.assert_not_called()
        push.assert_called_once()

    def test_exits_five_when_nuget_publish_fails(self, cli, config_path):
        args = _release_args(config_path, do_upload=False, do_nuget=True)

        with (
            mock.patch.object(
                cli, "collect_nupkgs", return_value=[Path("/tmp/GodotSharp.nupkg")]
            ),
            mock.patch.object(cli, "nuget_token_from_env", return_value="tok"),
            mock.patch.object(cli, "delete_nupkg_versions"),
            mock.patch.object(
                cli, "publish_nupkgs", side_effect=cli.PublishError("boom")
            ),
        ):
            rc = cli.cmd_release(args)

        assert rc == 5


# ---------------------------------------------------------------------------
# release runs the cosmetic chown LAST (after publish), and it is non-fatal
# ---------------------------------------------------------------------------


class TestReleaseChownRunsLast:
    def test_chown_invoked_at_end_of_release(self, cli, config_path):
        # Even with build/package/upload/nuget all skipped, the release flow
        # still hands the outputs back to the user as its final step.
        args = _release_args(config_path, do_upload=False, do_nuget=False)

        with mock.patch.object(cli, "_chown_outputs") as chown:
            rc = cli.cmd_release(args)

        assert rc == 0
        chown.assert_called_once()
        assert chown.call_args.kwargs.get("dry_run") is False

    def test_chown_runs_after_publish_and_nuget(self, cli, config_path):
        # Ordering guarantee: the chown is the tail of the flow, after both the
        # release upload and the NuGet push — so an interruption there cannot
        # cost the publish.
        calls: list[str] = []
        args = _release_args(config_path, do_upload=True, do_nuget=True)

        with (
            mock.patch.object(
                cli, "collect_release_assets", return_value=[Path("/tmp/a.zip")]
            ),
            mock.patch.object(
                cli, "publish_release", side_effect=lambda **k: calls.append("upload")
            ),
            mock.patch.object(
                cli, "collect_nupkgs", return_value=[Path("/tmp/GodotSharp.nupkg")]
            ),
            mock.patch.object(cli, "nuget_token_from_env", return_value="tok"),
            mock.patch.object(
                cli, "delete_nupkg_versions", side_effect=lambda **k: calls.append("nuget-del")
            ),
            mock.patch.object(
                cli, "publish_nupkgs", side_effect=lambda **k: calls.append("nuget-push")
            ),
            mock.patch.object(
                cli, "_chown_outputs", side_effect=lambda *a, **k: calls.append("chown")
            ),
        ):
            rc = cli.cmd_release(args)

        assert rc == 0
        assert calls[-1] == "chown"
        assert calls.index("upload") < calls.index("chown")
        assert calls.index("nuget-push") < calls.index("chown")


# ---------------------------------------------------------------------------
# release path Apple always builds — no host-side skip wiring
# ---------------------------------------------------------------------------


class TestReleaseAppleAlwaysBuilds:
    def test_release_dispatch_build_threads_no_apple_gate_kwarg(self, cli, config_path):
        # The release path must call dispatch_build without any apple-gate
        # kwarg: Apple targets always attempt to build, failures surface
        # in-container.
        captured = {}

        def fake_build(**kwargs):
            captured["kwargs"] = kwargs
            return 0

        args = _release_args(config_path, do_build=True, do_upload=False)

        with mock.patch.object(cli, "dispatch_build", side_effect=fake_build):
            rc = cli.cmd_release(args)

        assert rc == 0
        apple_gate_kwargs = [k for k in captured["kwargs"] if "apple" in k.lower()]
        assert apple_gate_kwargs == []

    def test_release_scopes_build_to_requested_platform_and_archs(
        self, cli, tmp_path
    ):
        # A single-platform scope threads the platform filter AND that
        # platform's configured archs into dispatch_build (arch scoping is a
        # Linux capability today).
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            'registry = "ghcr.io"\n'
            'username = "testuser"\n'
            'godot_version = "4.8"\n\n'
            "[[platforms]]\n"
            'name = "linux"\n'
            'image = "ghcr.io/test/linux:4.8"\n'
            'scons_flags = "platform=linuxbsd"\n'
            'archs = ["x86_64"]\n',
            encoding="utf-8",
        )
        captured = {}

        def fake_build(**kwargs):
            captured.update(kwargs)
            return 0

        args = _release_args(
            str(cfg), platform="linux", do_build=True, do_upload=False
        )
        with mock.patch.object(cli, "dispatch_build", side_effect=fake_build):
            rc = cli.cmd_release(args)

        assert rc == 0
        assert captured["platforms"] == ["linux"]
        assert captured["build_archs"] == ["x86_64"]

    def test_release_multi_platform_scope_keeps_all_archs(self, cli, config_path):
        # More than one platform -> build_archs is None (a single env cannot
        # express per-platform arch scopes).
        captured = {}

        def fake_build(**kwargs):
            captured.update(kwargs)
            return 0

        args = _release_args(
            config_path, platform="linux,windows", do_build=True, do_upload=False
        )
        with mock.patch.object(cli, "dispatch_build", side_effect=fake_build):
            rc = cli.cmd_release(args)

        assert rc == 0
        assert captured["platforms"] == ["linux", "windows"]
        assert captured["build_archs"] is None


# ---------------------------------------------------------------------------
# release --jobs override precedence (Directive 1: nproc-2 default, flag wins)
# ---------------------------------------------------------------------------


class TestReleaseJobsOverride:
    def _num_cores_for(self, cli, config_path, jobs):
        captured = {}

        def fake_build(**kwargs):
            captured["num_cores"] = kwargs["num_cores"]
            return 0

        args = _release_args(config_path, do_build=True, do_upload=False, jobs=jobs)

        with mock.patch.object(cli, "dispatch_build", side_effect=fake_build):
            cli.cmd_release(args)
        return captured["num_cores"]

    def test_explicit_jobs_flag_wins_over_config_default(self, cli, config_path):
        # --jobs 3 must reach the host orchestrator as -j3 regardless of the
        # nproc-2 default.
        assert self._num_cores_for(cli, config_path, jobs=3) == 3

    def test_falls_back_to_config_build_jobs_default_when_jobs_omitted(
        self, cli, config_path
    ):
        # With no --jobs the release path uses [build].build_jobs, whose default
        # is the dynamic nproc-2 value (floor 1) — never a hardcoded number.
        from scripts.config import DEFAULT_BUILD_JOBS

        assert self._num_cores_for(cli, config_path, jobs=None) == DEFAULT_BUILD_JOBS


# ---------------------------------------------------------------------------
# release build_type derivation from the mono matrix
# ---------------------------------------------------------------------------


class TestReleaseBuildType:
    def _build_type_for_mono(self, cli, config_path, mono_list):
        captured = {}

        def fake_build(**kwargs):
            captured["build_type"] = kwargs["build_type"]
            return 0

        toml = _MINIMAL_VALID_TOML + f"\n[build]\nmono = {mono_list}\n"
        Path(config_path).write_text(toml, encoding="utf-8")
        args = _release_args(config_path, do_build=True, do_upload=False)

        with mock.patch.object(cli, "dispatch_build", side_effect=fake_build):
            cli.cmd_release(args)
        return captured["build_type"]

    def test_build_type_all_when_both_mono_variants_configured(self, cli, config_path):
        assert self._build_type_for_mono(cli, config_path, '["on", "off"]') == "all"

    def test_build_type_mono_when_only_on_configured(self, cli, config_path):
        assert self._build_type_for_mono(cli, config_path, '["on"]') == "mono"

    def test_build_type_classical_when_only_off_configured(self, cli, config_path):
        assert self._build_type_for_mono(cli, config_path, '["off"]') == "classical"


# ---------------------------------------------------------------------------
# release threads config godot_version as the container image tag
# ---------------------------------------------------------------------------


class TestReleaseContainerVersion:
    def test_passes_config_godot_version_as_container_version(self, cli, config_path):
        # The host orchestrator resolves image tags before extracting the
        # engine version, so cmd_release must hand dispatch_build the config
        # godot_version (4.3 in the minimal test TOML) as container_version.
        # This makes the image refs godot-<plat>:<godot_version>, matching
        # config.toml / `containers --push`.
        captured = {}

        def fake_build(**kwargs):
            captured["container_version"] = kwargs["container_version"]
            return 0

        args = _release_args(config_path, do_build=True, do_upload=False)

        with mock.patch.object(cli, "dispatch_build", side_effect=fake_build):
            rc = cli.cmd_release(args)

        assert rc == 0
        assert captured["container_version"] == "4.3"
