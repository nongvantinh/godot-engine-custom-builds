"""Tests for ``scripts/in_container/common.py``.

Every external touchpoint is mocked — no real subprocess
invocations, no real network, no real swiftly install. Filesystem mutation is
constrained to ``tmp_path``.
"""

from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path
from unittest import mock

import pytest

from scripts.in_container import common

# ---------------------------------------------------------------------------
# env helpers
# ---------------------------------------------------------------------------


class TestEnvFlag:
    def test_returns_true_when_value_is_one(self, monkeypatch):
        monkeypatch.setenv("X", "1")
        assert common.env_flag("X") is True

    def test_returns_false_when_value_is_zero(self, monkeypatch):
        monkeypatch.setenv("X", "0")
        assert common.env_flag("X") is False

    def test_returns_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("X", raising=False)
        assert common.env_flag("X", default=True) is True
        assert common.env_flag("X", default=False) is False


class TestEnvNumCores:
    def test_parses_integer(self, monkeypatch):
        monkeypatch.setenv("NUM_CORES", "8")
        assert common.env_num_cores() == 8

    def test_clamps_to_one_minimum(self, monkeypatch):
        monkeypatch.setenv("NUM_CORES", "0")
        assert common.env_num_cores() == 1

    def test_returns_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("NUM_CORES", raising=False)
        assert common.env_num_cores(default=4) == 4

    def test_returns_default_when_not_an_integer(self, monkeypatch):
        monkeypatch.setenv("NUM_CORES", "abc")
        assert common.env_num_cores(default=2) == 2


class TestEnvBuildArchs:
    def test_returns_none_when_unset(self, monkeypatch):
        monkeypatch.delenv("GODOT_BUILD_ARCHS", raising=False)
        assert common.env_build_archs() is None

    def test_returns_none_when_empty(self, monkeypatch):
        monkeypatch.setenv("GODOT_BUILD_ARCHS", "  ")
        assert common.env_build_archs() is None

    def test_parses_single_arch(self, monkeypatch):
        monkeypatch.setenv("GODOT_BUILD_ARCHS", "x86_64")
        assert common.env_build_archs() == {"x86_64"}

    def test_parses_csv_and_strips(self, monkeypatch):
        monkeypatch.setenv("GODOT_BUILD_ARCHS", " x86_64 , arm64 ")
        assert common.env_build_archs() == {"x86_64", "arm64"}


# ---------------------------------------------------------------------------
# run_scons
# ---------------------------------------------------------------------------


class TestRunScons:
    def test_prepends_shared_flags_and_threads(self):
        with mock.patch("scripts.in_container.common.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0)
            common.run_scons("platform=linuxbsd", "target=editor", num_cores=8)

        cmd = run.call_args.args[0]
        assert cmd[0] == "scons"
        assert cmd[1] == "-j8"
        # SCONS_COMMON_FLAGS appears next, in order.
        assert (
            tuple(cmd[2 : 2 + len(common.SCONS_COMMON_FLAGS)])
            == common.SCONS_COMMON_FLAGS
        )
        # Then user args.
        assert cmd[-2:] == ["platform=linuxbsd", "target=editor"]

    def test_raises_when_scons_exits_nonzero_with_check(self):
        with mock.patch("scripts.in_container.common.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 5)
            with pytest.raises(common.InContainerBuildError):
                common.run_scons("target=editor", num_cores=2)

    def test_returns_rc_when_check_false(self):
        with mock.patch("scripts.in_container.common.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 5)
            rc = common.run_scons("target=editor", num_cores=2, check=False)
        assert rc == 5

    def test_passes_env_when_provided(self):
        with mock.patch("scripts.in_container.common.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0)
            common.run_scons("target=editor", num_cores=1, env={"PATH": "/sdk/bin"})
        assert run.call_args.kwargs["env"] == {"PATH": "/sdk/bin"}


# ---------------------------------------------------------------------------
# extract_tarball / setup_godot_source
# ---------------------------------------------------------------------------


def _make_tarball(path: Path, members: dict[str, str]) -> None:
    """Write a tar.gz at *path* with *members* dict (name -> content)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    src_dir = path.parent / "_make_src"
    src_dir.mkdir(exist_ok=True)
    with tarfile.open(path, "w:gz") as tf:
        for name, content in members.items():
            tmp = src_dir / Path(name).name
            tmp.write_text(content)
            tf.add(tmp, arcname=name)


class TestExtractTarball:
    def test_strips_leading_component(self, tmp_path):
        tar = tmp_path / "src.tar.gz"
        _make_tarball(tar, {"godot-4.8/version.py": "x", "godot-4.8/SConstruct": "y"})

        dest = tmp_path / "out"
        common.extract_tarball(tar, dest, strip_components=1)

        assert (dest / "version.py").read_text() == "x"
        assert (dest / "SConstruct").read_text() == "y"
        # The "godot-4.8/" prefix is stripped — no nested dir.
        assert not (dest / "godot-4.8").exists()

    def test_raises_for_missing_tarball(self, tmp_path):
        with pytest.raises(common.InContainerBuildError):
            common.extract_tarball(tmp_path / "missing.tar.gz", tmp_path / "out")


class TestSetupGodotSource:
    def test_removes_stale_tree_and_extracts(self, tmp_path):
        tar = tmp_path / "godot.tar.gz"
        _make_tarball(tar, {"godot-4.8/version.py": "v"})

        # Pre-existing godot/ with junk should be wiped.
        stale = tmp_path / "root" / "godot"
        stale.mkdir(parents=True)
        (stale / "junk.txt").write_text("old")

        result = common.setup_godot_source(tarball=tar, dest_root=tmp_path / "root")

        assert result == tmp_path / "root" / "godot"
        assert (result / "version.py").read_text() == "v"
        assert not (result / "junk.txt").exists()


# ---------------------------------------------------------------------------
# copy_mono_glue
# ---------------------------------------------------------------------------


class TestCopyMonoGlue:
    def test_copies_runtime_and_editor_generated_dirs(self, tmp_path):
        source = tmp_path / "mono-glue"
        (source / "GodotSharp" / "GodotSharp" / "Generated").mkdir(parents=True)
        (source / "GodotSharp" / "GodotSharp" / "Generated" / "Runtime.cs").write_text(
            "class R{}"
        )
        (source / "GodotSharp" / "GodotSharpEditor" / "Generated").mkdir(parents=True)
        (
            source / "GodotSharp" / "GodotSharpEditor" / "Generated" / "Editor.cs"
        ).write_text("class E{}")

        godot = tmp_path / "godot"
        godot.mkdir()
        common.copy_mono_glue(source, godot, include_editor=True)

        runtime_target = (
            godot
            / "modules"
            / "mono"
            / "glue"
            / "GodotSharp"
            / "GodotSharp"
            / "Generated"
            / "Runtime.cs"
        )
        editor_target = (
            godot
            / "modules"
            / "mono"
            / "glue"
            / "GodotSharp"
            / "GodotSharpEditor"
            / "Generated"
            / "Editor.cs"
        )
        assert runtime_target.read_text() == "class R{}"
        assert editor_target.read_text() == "class E{}"

    def test_skips_editor_glue_when_not_requested(self, tmp_path):
        source = tmp_path / "mono-glue"
        (source / "GodotSharp" / "GodotSharp" / "Generated").mkdir(parents=True)
        (source / "GodotSharp" / "GodotSharp" / "Generated" / "Runtime.cs").write_text(
            "x"
        )
        # No GodotSharpEditor dir on disk.

        godot = tmp_path / "godot"
        godot.mkdir()
        common.copy_mono_glue(source, godot, include_editor=False)

        editor_dir = (
            godot / "modules" / "mono" / "glue" / "GodotSharp" / "GodotSharpEditor"
        )
        assert not editor_dir.exists()

    def test_raises_when_runtime_glue_missing(self, tmp_path):
        source = tmp_path / "mono-glue"
        source.mkdir()
        godot = tmp_path / "godot"
        godot.mkdir()
        with pytest.raises(common.InContainerBuildError):
            common.copy_mono_glue(source, godot)


# ---------------------------------------------------------------------------
# copy_and_clean_bin
# ---------------------------------------------------------------------------


class TestCopyAndCleanBin:
    def test_moves_entries_to_dest_and_removes_source(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "godot.exe").write_text("E")
        (bin_dir / "subdir").mkdir()
        (bin_dir / "subdir" / "file.dll").write_text("D")

        dest = tmp_path / "out"
        common.copy_and_clean_bin(bin_dir, dest)

        assert (dest / "godot.exe").read_text() == "E"
        assert (dest / "subdir" / "file.dll").read_text() == "D"
        assert not (bin_dir / "godot.exe").exists()
        assert not (bin_dir / "subdir").exists()

    def test_preserves_build_deps_by_default(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "build_deps").mkdir()
        (bin_dir / "build_deps" / "agility.dll").write_text("A")
        (bin_dir / "godot.exe").write_text("E")

        dest = tmp_path / "out"
        common.copy_and_clean_bin(bin_dir, dest)

        # godot.exe moved, build_deps/ stayed put.
        assert (dest / "godot.exe").is_file()
        assert (bin_dir / "build_deps" / "agility.dll").read_text() == "A"
        assert not (dest / "build_deps").exists()

    def test_handles_missing_bin_gracefully(self, tmp_path):
        # Over a missing dir: no-op.
        common.copy_and_clean_bin(tmp_path / "missing-bin", tmp_path / "out")


class TestCleanBinPreserving:
    def test_wipes_non_preserved_entries(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "build_deps").mkdir()
        (bin_dir / "build_deps" / "agility.dll").write_text("A")
        (bin_dir / "godot.exe").write_text("E")
        (bin_dir / "stale").mkdir()
        (bin_dir / "stale" / "a.o").write_text("x")

        common.clean_bin_preserving(bin_dir)

        assert not (bin_dir / "godot.exe").exists()
        assert not (bin_dir / "stale").exists()
        # build_deps preserved.
        assert (bin_dir / "build_deps" / "agility.dll").is_file()


# ---------------------------------------------------------------------------
# install_d3d12_sdk
# ---------------------------------------------------------------------------


class TestInstallD3d12Sdk:
    def _stage_godot_with_installer(self, tmp_path: Path) -> Path:
        godot = tmp_path / "godot"
        (godot / "misc" / "scripts").mkdir(parents=True)
        (godot / "misc" / "scripts" / "install_d3d12_sdk_windows.py").write_text(
            "# fake"
        )
        return godot

    def test_succeeds_on_first_attempt(self, tmp_path):
        godot = self._stage_godot_with_installer(tmp_path)
        sleeps: list[float] = []
        with (
            mock.patch("scripts.in_container.common.subprocess.run") as run,
            mock.patch(
                "scripts.in_container.common.shutil.which",
                return_value="/usr/bin/python3",
            ),
        ):
            run.return_value = subprocess.CompletedProcess([], 0)
            common.install_d3d12_sdk(godot, sleep_fn=sleeps.append)

        # One subprocess call, no sleeps.
        assert run.call_count == 1
        assert sleeps == []
        cmd = run.call_args.args[0]
        assert cmd[0] == "/usr/bin/python3"
        assert cmd[1].endswith("install_d3d12_sdk_windows.py")
        assert any(a.startswith("--mingw_prefix=") for a in cmd)

    def test_retries_on_failure_and_then_succeeds(self, tmp_path):
        godot = self._stage_godot_with_installer(tmp_path)
        sleeps: list[float] = []
        with (
            mock.patch("scripts.in_container.common.subprocess.run") as run,
            mock.patch(
                "scripts.in_container.common.shutil.which",
                return_value="/usr/bin/python3",
            ),
        ):
            run.side_effect = [
                subprocess.CompletedProcess([], 7),
                subprocess.CompletedProcess([], 0),
            ]
            common.install_d3d12_sdk(godot, attempts=4, sleep_fn=sleeps.append)

        assert run.call_count == 2
        # One sleep between attempt 1 and 2.
        assert len(sleeps) == 1
        assert sleeps[0] == common._D3D12_BACKOFF_S

    def test_raises_after_exhausting_attempts(self, tmp_path):
        godot = self._stage_godot_with_installer(tmp_path)
        sleeps: list[float] = []
        with (
            mock.patch("scripts.in_container.common.subprocess.run") as run,
            mock.patch(
                "scripts.in_container.common.shutil.which",
                return_value="/usr/bin/python3",
            ),
        ):
            run.return_value = subprocess.CompletedProcess([], 3)
            with pytest.raises(common.InContainerBuildError):
                common.install_d3d12_sdk(godot, attempts=2, sleep_fn=sleeps.append)

        assert run.call_count == 2
        # One sleep between the two attempts; no sleep after the final one.
        assert len(sleeps) == 1

    def test_retries_on_timeout(self, tmp_path):
        godot = self._stage_godot_with_installer(tmp_path)
        sleeps: list[float] = []
        with (
            mock.patch("scripts.in_container.common.subprocess.run") as run,
            mock.patch(
                "scripts.in_container.common.shutil.which",
                return_value="/usr/bin/python3",
            ),
        ):
            run.side_effect = [
                subprocess.TimeoutExpired(cmd=["python3"], timeout=600),
                subprocess.CompletedProcess([], 0),
            ]
            common.install_d3d12_sdk(godot, attempts=4, sleep_fn=sleeps.append)

        assert run.call_count == 2

    def test_raises_when_installer_missing(self, tmp_path):
        # No misc/scripts/install_d3d12_sdk_windows.py — same hard error bash had.
        godot = tmp_path / "godot"
        godot.mkdir()
        with pytest.raises(common.InContainerBuildError):
            common.install_d3d12_sdk(godot)


# ---------------------------------------------------------------------------
# swiftly_install
# ---------------------------------------------------------------------------


class TestSwiftlyInstall:
    def _stage_swiftly_bin(self, tmp_path: Path) -> Path:
        swiftly_home = tmp_path / "swiftly"
        swiftly_bin_dir = swiftly_home / "bin"
        swiftly_bin_dir.mkdir(parents=True)
        swiftly_bin = swiftly_bin_dir / "swiftly"
        swiftly_bin.write_text("#!/bin/sh\nexit 0\n")
        swiftly_bin.chmod(0o755)
        return swiftly_home

    def test_skips_install_when_swift_frontend_already_present(self, tmp_path):
        swiftly_home = self._stage_swiftly_bin(tmp_path)
        # Pre-stage swift-frontend.
        sf = swiftly_home / "toolchains" / "6.2.1" / "usr" / "bin" / "swift-frontend"
        sf.parent.mkdir(parents=True)
        sf.write_text("#!/bin/sh\nexit 0\n")
        sf.chmod(0o755)

        with mock.patch("scripts.in_container.common.subprocess.run") as run:
            result = common.swiftly_install("6.2.1", swiftly_home=swiftly_home)

        run.assert_not_called()
        assert result == swiftly_home / "toolchains" / "6.2.1"

    def test_invokes_swiftly_when_swift_frontend_missing(self, tmp_path):
        swiftly_home = self._stage_swiftly_bin(tmp_path)
        # No swift-frontend yet — first call must invoke swiftly. We simulate
        # the install creating the binary on success.
        sf = swiftly_home / "toolchains" / "6.2.1" / "usr" / "bin" / "swift-frontend"

        def fake_run(cmd, *args, **kwargs):
            # Stage swift-frontend so the post-install check passes.
            sf.parent.mkdir(parents=True, exist_ok=True)
            sf.write_text("#!/bin/sh\nexit 0\n")
            sf.chmod(0o755)
            return subprocess.CompletedProcess(cmd, 0)

        with mock.patch(
            "scripts.in_container.common.subprocess.run", side_effect=fake_run
        ) as run:
            result = common.swiftly_install("6.2.1", swiftly_home=swiftly_home)

        assert run.call_count == 1
        cmd = run.call_args.args[0]
        # Non-interactive + post-install discard.
        assert "--assume-yes" in cmd
        assert "--post-install-file=/dev/null" in cmd
        assert "6.2.1" in cmd
        assert result == swiftly_home / "toolchains" / "6.2.1"

    def test_swiftly_install_verifies_gpg_signatures(self, tmp_path):
        # Swift 6.2.1 is pre-installed at image-build time in
        # containers/Dockerfile.osx with full PGP verification, and
        # gpgconf --kill all clears stale gpg-agent sockets. If a
        # non-default SWIFT_VERSION is ever requested, swiftly's runtime
        # install MUST verify against the host /root/.gnupg/ keyring — never
        # pass --no-verify.
        swiftly_home = self._stage_swiftly_bin(tmp_path)
        sf = swiftly_home / "toolchains" / "6.2.1" / "usr" / "bin" / "swift-frontend"

        def fake_run(cmd, *args, **kwargs):
            sf.parent.mkdir(parents=True, exist_ok=True)
            sf.write_text("#!/bin/sh\nexit 0\n")
            sf.chmod(0o755)
            return subprocess.CompletedProcess(cmd, 0)

        with mock.patch(
            "scripts.in_container.common.subprocess.run", side_effect=fake_run
        ) as run:
            common.swiftly_install("6.2.1", swiftly_home=swiftly_home)

        cmd = run.call_args.args[0]
        assert "--no-verify" not in cmd, "swiftly install must verify GPG signatures."

    def test_raises_when_swiftly_binary_missing(self, tmp_path):
        # No swiftly bin staged.
        with pytest.raises(common.InContainerBuildError):
            common.swiftly_install("6.2.1", swiftly_home=tmp_path / "swiftly")

    def test_raises_when_install_reports_success_but_swift_frontend_absent(
        self, tmp_path
    ):
        swiftly_home = self._stage_swiftly_bin(tmp_path)
        with mock.patch(
            "scripts.in_container.common.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0),
        ):
            with pytest.raises(common.InContainerBuildError):
                common.swiftly_install("6.2.1", swiftly_home=swiftly_home)


# ---------------------------------------------------------------------------
# apply_swappy
# ---------------------------------------------------------------------------


class TestApplySwappy:
    def test_copies_swappy_contents_into_thirdparty(self, tmp_path):
        swappy = tmp_path / "swappy"
        (swappy / "include").mkdir(parents=True)
        (swappy / "include" / "swappy.h").write_text("h")
        (swappy / "lib.a").write_text("lib")

        godot = tmp_path / "godot"
        godot.mkdir()
        common.apply_swappy(swappy, godot)

        target = godot / "thirdparty" / "swappy-frame-pacing"
        assert (target / "include" / "swappy.h").read_text() == "h"
        assert (target / "lib.a").read_text() == "lib"

    def test_raises_when_swappy_dir_missing(self, tmp_path):
        with pytest.raises(common.InContainerBuildError):
            common.apply_swappy(tmp_path / "nope", tmp_path / "godot")


# ---------------------------------------------------------------------------
# build_mono_assemblies / gradle_wrapper
# ---------------------------------------------------------------------------


class TestBuildMonoAssemblies:
    def test_invokes_build_assemblies_with_platform(self, tmp_path):
        godot = tmp_path / "godot"
        script = godot / "modules" / "mono" / "build_scripts" / "build_assemblies.py"
        script.parent.mkdir(parents=True)
        script.write_text("# fake")

        with (
            mock.patch("scripts.in_container.common.subprocess.run") as run,
            mock.patch(
                "scripts.in_container.common.shutil.which",
                return_value="/usr/bin/python3",
            ),
        ):
            run.return_value = subprocess.CompletedProcess([], 0)
            common.build_mono_assemblies(godot, godot_platform="linuxbsd")

        cmd = run.call_args.args[0]
        assert cmd[1] == str(script)
        assert "--godot-output-dir=./bin" in cmd
        assert "--godot-platform=linuxbsd" in cmd


class TestGradleWrapper:
    def test_invokes_gradlew_with_task(self, tmp_path):
        godot = tmp_path / "godot"
        gradlew = godot / "platform" / "android" / "java" / "gradlew"
        gradlew.parent.mkdir(parents=True)
        gradlew.write_text("#!/bin/sh\nexit 0\n")
        gradlew.chmod(0o755)

        with mock.patch("scripts.in_container.common.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0)
            common.gradle_wrapper(godot, "generateGodotEditor")

        cmd = run.call_args.args[0]
        assert cmd == [str(gradlew), "generateGodotEditor"]
        assert run.call_args.kwargs["cwd"] == str(gradlew.parent)
