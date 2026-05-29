"""Tests for the per-platform in-container build entry points.

Each platform module ``main()`` is exercised under different env contracts
(CLASSICAL=0/1, MONO=0/1, NUM_CORES, platform-specific knobs).

The shared mocking pattern: patch every helper in ``scripts.in_container.common``
that talks to the world (run_scons, install_d3d12_sdk, swiftly_install,
build_mono_assemblies, gradle_wrapper, etc.) so the tests assert on which
helpers were called and with what args.

We also patch ``setup_godot_source`` / ``copy_mono_glue`` / ``apply_swappy`` so
the per-platform main() never touches the real filesystem outside ``tmp_path``.
"""

from __future__ import annotations

import subprocess
from unittest import mock

import pytest

from scripts.in_container import (
    build_android,
    build_ios,
    build_linux,
    build_macos,
    build_mono_glue,
    build_web,
    build_windows,
)


# ---------------------------------------------------------------------------
# Shared fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def base_env(monkeypatch, tmp_path):
    """Set sensible defaults for the env contract every module reads."""
    monkeypatch.setenv("NUM_CORES", "2")
    monkeypatch.setenv("BUILD_NAME", "official")
    monkeypatch.setenv("GODOT_VERSION_STATUS", "dev1")
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setenv("BASE_PATH", "/usr/bin")
    # Default to classical only; individual tests override.
    monkeypatch.setenv("CLASSICAL", "1")
    monkeypatch.setenv("MONO", "0")
    monkeypatch.setenv("STEAM", "0")
    # Redirect /root/out, /root/mono-glue, /root/swappy away from the real
    # paths so the modules never need root privileges to mkdir.
    out_root = tmp_path / "out"
    out_root.mkdir()
    monkeypatch.setenv("OUT_ROOT", str(out_root))
    mono_glue = tmp_path / "mono-glue"
    mono_glue.mkdir()
    monkeypatch.setenv("MONO_GLUE_DIR", str(mono_glue))
    swappy = tmp_path / "swappy"
    swappy.mkdir()
    (swappy / "stub.h").write_text("stub")
    monkeypatch.setenv("SWAPPY_DIR", str(swappy))
    return monkeypatch


def _patch_module(module_name: str, **kwargs):
    """Shorthand for nested mock.patch usage."""
    return mock.patch.multiple(f"scripts.in_container.{module_name}", **kwargs)


def _scons_call_args(run_scons_mock) -> list[tuple[str, ...]]:
    """Return the positional ``*args`` (the scons flags) for every call."""
    return [tuple(call.args) for call in run_scons_mock.call_args_list]


# ---------------------------------------------------------------------------
# build_mono_glue
# ---------------------------------------------------------------------------


class TestBuildMonoGlue:
    def test_mono_zero_skips_glue_generation(self, base_env, tmp_path):
        base_env.setenv("MONO", "0")
        # setup_godot_source is still called for parity with bash's `rm -rf
        # godot && tar xf ...` ritual, so we must stub it to a tmp dir.
        with (
            mock.patch.object(
                build_mono_glue.common,
                "setup_godot_source",
                return_value=tmp_path / "godot",
            ),
            mock.patch.object(build_mono_glue.common, "run_scons") as run_scons,
            mock.patch("scripts.in_container.build_mono_glue.subprocess.run") as run,
        ):
            rc = build_mono_glue.main([])

        assert rc == 0
        run_scons.assert_not_called()
        # No subprocess at all when MONO=0.
        run.assert_not_called()

    def test_mono_one_runs_scons_then_generate_mono_glue(self, base_env, tmp_path):
        base_env.setenv("MONO", "1")
        base_env.setenv("GODOT_SDK_LINUX_X86_64", "/sdk/x86_64")
        godot = tmp_path / "godot"
        # Stage the binary scons would have produced.
        bin_dir = godot / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "godot.linuxbsd.editor.x86_64.mono").write_text("#")

        with (
            mock.patch.object(
                build_mono_glue.common, "setup_godot_source", return_value=godot
            ),
            mock.patch.object(build_mono_glue.common, "run_scons") as run_scons,
            mock.patch("scripts.in_container.build_mono_glue.subprocess.run") as run,
            mock.patch(
                "scripts.in_container.build_mono_glue.shutil.which",
                return_value="/usr/bin/dotnet",
            ),
        ):
            run.return_value = subprocess.CompletedProcess([], 0)
            rc = build_mono_glue.main([])

        assert rc == 0
        # The scons call must include all expected flags.
        scons_args = _scons_call_args(run_scons)
        assert len(scons_args) == 1
        flags = scons_args[0]
        assert "platform=linuxbsd" in flags
        assert "target=editor" in flags
        assert "module_mono_enabled=yes" in flags
        assert "module_dotnet_enabled=yes" in flags
        # num_cores wired through kwargs
        assert run_scons.call_args.kwargs["num_cores"] == 2

        # The second subprocess invocation is the godot binary --generate-mono-glue.
        # The first was `dotnet --info`.
        all_calls = run.call_args_list
        glue_call = [c for c in all_calls if "--generate-mono-glue" in c.args[0]]
        assert len(glue_call) == 1
        cmd = glue_call[0].args[0]
        assert cmd[1] == "--headless"
        assert cmd[2] == "--generate-mono-glue"

    def test_uses_sdk_for_path_when_provided(self, base_env, tmp_path, monkeypatch):
        base_env.setenv("MONO", "1")
        base_env.setenv("GODOT_SDK_LINUX_X86_64", "/sdk/x86_64")
        base_env.setenv("BASE_PATH", "/usr/bin:/bin")
        godot = tmp_path / "godot"
        bin_dir = godot / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "godot.linuxbsd.editor.x86_64.mono").write_text("#")

        captured_env = {}

        def fake_run_scons(*args, **kwargs):
            captured_env.update(kwargs.get("env") or {})
            return 0

        with (
            mock.patch.object(
                build_mono_glue.common, "setup_godot_source", return_value=godot
            ),
            mock.patch.object(
                build_mono_glue.common, "run_scons", side_effect=fake_run_scons
            ),
            mock.patch("scripts.in_container.build_mono_glue.subprocess.run") as run,
            mock.patch(
                "scripts.in_container.build_mono_glue.shutil.which",
                return_value=None,
            ),
        ):
            run.return_value = subprocess.CompletedProcess([], 0)
            build_mono_glue.main([])

        assert captured_env["PATH"] == "/sdk/x86_64/bin:/usr/bin:/bin"


# ---------------------------------------------------------------------------
# build_linux
# ---------------------------------------------------------------------------


class TestBuildLinux:
    def test_classical_runs_three_archs_with_buildroot_path(
        self, base_env, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("GODOT_SDK_LINUX_X86_64", "/sdk/x86_64")
        monkeypatch.setenv("GODOT_SDK_LINUX_X86_32", "/sdk/x86_32")
        monkeypatch.setenv("GODOT_SDK_LINUX_ARM64", "/sdk/arm64")
        monkeypatch.setenv("GODOT_SDK_LINUX_ARM32", "/sdk/arm32")
        base_env.setenv("MONO", "0")
        godot = tmp_path / "godot"
        (godot / "bin").mkdir(parents=True)

        with (
            mock.patch.object(
                build_linux.common, "setup_godot_source", return_value=godot
            ),
            mock.patch.object(
                build_linux.common, "run_scons", return_value=0
            ) as run_scons,
            mock.patch("scripts.in_container.build_linux._copy_bin_and_clean"),
        ):
            # Stage a couple of files in bin so the copy helper has something to do.
            (godot / "bin" / "godot.linuxbsd").write_text("e")
            rc = build_linux.main([])

        assert rc == 0
        scons_args = _scons_call_args(run_scons)
        # 4 archs * 3 invocations (editor + template_debug + template_release) = 12.
        assert len(scons_args) == 12
        # Every call must carry `platform=linuxbsd` and the production OPTIONS.
        for args in scons_args:
            assert "platform=linuxbsd" in args
            assert "production=yes" in args
            assert "accesskit_sdk_path=/root/accesskit/accesskit-c" in args

        # Per-arch env: PATH is set to ${SDK}/bin:${BASE_PATH}.
        envs = [call.kwargs.get("env") for call in run_scons.call_args_list]
        x86_64_envs = [e for e, a in zip(envs, scons_args) if "arch=x86_64" in a]
        assert all(e["PATH"].startswith("/sdk/x86_64/bin:") for e in x86_64_envs)

    def test_mono_one_requires_glue_and_runs_assemblies(
        self, base_env, tmp_path, monkeypatch
    ):
        for var in (
            "GODOT_SDK_LINUX_X86_64",
            "GODOT_SDK_LINUX_X86_32",
            "GODOT_SDK_LINUX_ARM64",
            "GODOT_SDK_LINUX_ARM32",
        ):
            monkeypatch.setenv(var, f"/sdk/{var}")
        base_env.setenv("CLASSICAL", "0")
        base_env.setenv("MONO", "1")
        godot = tmp_path / "godot"
        (godot / "bin").mkdir(parents=True)

        with (
            mock.patch.object(
                build_linux.common, "setup_godot_source", return_value=godot
            ),
            mock.patch.object(
                build_linux.common, "run_scons", return_value=0
            ) as run_scons,
            mock.patch.object(build_linux.common, "copy_mono_glue") as copy_glue,
            mock.patch.object(
                build_linux.common, "build_mono_assemblies"
            ) as build_assemblies,
            mock.patch("scripts.in_container.build_linux._copy_bin_and_clean"),
        ):
            rc = build_linux.main([])

        assert rc == 0
        # Mono glue copied once before any per-arch loop iteration.
        copy_glue.assert_called_once()
        assert copy_glue.call_args.kwargs.get("include_editor") is True

        # build_assemblies runs ONCE per arch (after the editor pass).
        assert build_assemblies.call_count == 4
        for call in build_assemblies.call_args_list:
            assert call.kwargs["godot_platform"] == "linuxbsd"

        # Each scons call must include both OPTIONS and OPTIONS_MONO.
        for args in _scons_call_args(run_scons):
            assert "module_mono_enabled=yes" in args
            assert "module_dotnet_enabled=yes" in args


# ---------------------------------------------------------------------------
# build_windows
# ---------------------------------------------------------------------------


class TestBuildWindows:
    def test_install_d3d12_runs_before_any_scons(self, base_env, tmp_path):
        base_env.setenv("MONO", "0")
        godot = tmp_path / "godot"
        (godot / "bin").mkdir(parents=True)
        order: list[str] = []

        with (
            mock.patch.object(
                build_windows.common, "setup_godot_source", return_value=godot
            ),
            mock.patch.object(
                build_windows.common,
                "install_d3d12_sdk",
                side_effect=lambda *a, **kw: order.append("d3d12"),
            ),
            mock.patch.object(
                build_windows.common,
                "run_scons",
                side_effect=lambda *a, **kw: (order.append("scons"), 0)[1],
            ),
            mock.patch.object(build_windows.common, "copy_and_clean_bin"),
            mock.patch.object(build_windows.common, "clean_bin_preserving"),
        ):
            build_windows.main([])

        # d3d12 must precede every scons call.
        assert order[0] == "d3d12"
        assert order.count("d3d12") == 1
        assert all(step == "scons" for step in order[1:])

    def test_arm64_classical_failure_is_best_effort(self, base_env, tmp_path, caplog):
        base_env.setenv("MONO", "0")
        godot = tmp_path / "godot"
        (godot / "bin").mkdir(parents=True)

        # Simulate scons failing for any arm64 invocation, passing for x86_*.
        def fake_scons(*args, **kwargs):
            arch = next((a for a in args if a.startswith("arch=")), "")
            if arch == "arch=arm64":
                return 1
            return 0

        with (
            mock.patch.object(
                build_windows.common, "setup_godot_source", return_value=godot
            ),
            mock.patch.object(build_windows.common, "install_d3d12_sdk"),
            mock.patch.object(
                build_windows.common, "run_scons", side_effect=fake_scons
            ),
            mock.patch.object(build_windows.common, "copy_and_clean_bin"),
            mock.patch.object(build_windows.common, "clean_bin_preserving"),
            caplog.at_level("WARNING"),
        ):
            rc = build_windows.main([])

        # Best-effort: failure is non-fatal.
        assert rc == 0
        assert any(
            "arm64 classical Windows build failed" in r.message for r in caplog.records
        )

    def test_steam_build_runs_when_steam_flag_set(self, base_env, tmp_path):
        base_env.setenv("MONO", "0")
        base_env.setenv("STEAM", "1")
        godot = tmp_path / "godot"
        (godot / "bin").mkdir(parents=True)

        with (
            mock.patch.object(
                build_windows.common, "setup_godot_source", return_value=godot
            ),
            mock.patch.object(build_windows.common, "install_d3d12_sdk"),
            mock.patch.object(
                build_windows.common, "run_scons", return_value=0
            ) as run_scons,
            mock.patch.object(build_windows.common, "copy_and_clean_bin"),
            mock.patch.object(build_windows.common, "clean_bin_preserving"),
        ):
            build_windows.main([])

        steam_calls = [
            args for args in _scons_call_args(run_scons) if "steamapi=yes" in args
        ]
        # Two steam editor builds: x86_64 + x86_32.
        assert len(steam_calls) == 2

    def test_mono_build_invokes_copy_glue_and_assemblies(self, base_env, tmp_path):
        base_env.setenv("CLASSICAL", "0")
        base_env.setenv("MONO", "1")
        godot = tmp_path / "godot"
        (godot / "bin").mkdir(parents=True)

        with (
            mock.patch.object(
                build_windows.common, "setup_godot_source", return_value=godot
            ),
            mock.patch.object(build_windows.common, "install_d3d12_sdk"),
            mock.patch.object(build_windows.common, "run_scons", return_value=0),
            mock.patch.object(build_windows.common, "copy_mono_glue") as copy_glue,
            mock.patch.object(
                build_windows.common, "build_mono_assemblies"
            ) as build_assemblies,
            mock.patch.object(build_windows.common, "copy_and_clean_bin"),
            mock.patch.object(build_windows.common, "clean_bin_preserving"),
        ):
            build_windows.main([])

        copy_glue.assert_called_once()
        assert copy_glue.call_args.kwargs.get("include_editor") is True
        # Mono assemblies run per-arch editor pass (x86_64, x86_32; arm64 only
        # if the arm64 editor succeeded — here it does because run_scons
        # always returns 0).
        assert build_assemblies.call_count >= 2


# ---------------------------------------------------------------------------
# build_android
# ---------------------------------------------------------------------------


class TestBuildAndroid:
    def test_no_keystore_logs_unsigned_and_sets_store_release_no(
        self, base_env, tmp_path, caplog
    ):
        # Keystore env vars explicitly unset.
        for var in (
            "GODOT_ANDROID_SIGN_KEYSTORE",
            "GODOT_ANDROID_SIGN_KEY_ALIAS",
            "GODOT_ANDROID_SIGN_PASSWORD",
        ):
            base_env.delenv(var, raising=False)
        base_env.setenv("MONO", "0")
        godot = tmp_path / "godot"
        (godot / "bin").mkdir(parents=True)

        captured_args: list[tuple[str, ...]] = []

        def fake_scons(*args, **kwargs):
            captured_args.append(args)
            return 0

        with (
            mock.patch.object(
                build_android.common, "setup_godot_source", return_value=godot
            ),
            mock.patch.object(build_android.common, "apply_swappy"),
            mock.patch.object(
                build_android.common, "run_scons", side_effect=fake_scons
            ),
            mock.patch.object(build_android.common, "gradle_wrapper"),
            mock.patch.object(build_android.common, "copy_mono_glue"),
            caplog.at_level("INFO"),
        ):
            rc = build_android.main([])

        assert rc == 0
        assert any(
            "using debug build instead" in r.message.lower() for r in caplog.records
        )
        # Every editor scons call carries store_release=no.
        editor_calls = [args for args in captured_args if "target=editor" in args]
        for args in editor_calls:
            assert "store_release=no" in args

    def test_keystore_present_sets_store_release_yes(self, base_env, tmp_path):
        base_env.setenv("GODOT_ANDROID_SIGN_KEYSTORE", "/keystore/release.keystore")
        base_env.setenv("GODOT_ANDROID_SIGN_KEY_ALIAS", "release")
        base_env.setenv("GODOT_ANDROID_SIGN_PASSWORD", "secret")
        base_env.setenv("MONO", "0")
        godot = tmp_path / "godot"
        (godot / "bin").mkdir(parents=True)

        captured: list[tuple[str, ...]] = []
        with (
            mock.patch.object(
                build_android.common, "setup_godot_source", return_value=godot
            ),
            mock.patch.object(build_android.common, "apply_swappy"),
            mock.patch.object(
                build_android.common,
                "run_scons",
                side_effect=lambda *a, **kw: (captured.append(a), 0)[1],
            ),
            mock.patch.object(build_android.common, "gradle_wrapper"),
            # When store_release=yes the script copies the godot tree to
            # out/source for MavenCentral upload; we patch shutil.copytree
            # because the source tree only has bin/ in this test.
            mock.patch("scripts.in_container.build_android.shutil.copytree"),
            mock.patch("scripts.in_container.build_android.shutil.rmtree"),
            mock.patch("scripts.in_container.build_android._copy_template_outputs"),
        ):
            build_android.main([])

        editor_calls = [args for args in captured if "target=editor" in args]
        for args in editor_calls:
            assert "store_release=yes" in args

    def test_classical_runs_four_archs_for_editor_and_templates(
        self, base_env, tmp_path
    ):
        base_env.setenv("MONO", "0")
        for var in (
            "GODOT_ANDROID_SIGN_KEYSTORE",
            "GODOT_ANDROID_SIGN_KEY_ALIAS",
            "GODOT_ANDROID_SIGN_PASSWORD",
        ):
            base_env.delenv(var, raising=False)

        godot = tmp_path / "godot"
        (godot / "bin").mkdir(parents=True)

        with (
            mock.patch.object(
                build_android.common, "setup_godot_source", return_value=godot
            ),
            mock.patch.object(build_android.common, "apply_swappy"),
            mock.patch.object(
                build_android.common, "run_scons", return_value=0
            ) as run_scons,
            mock.patch.object(build_android.common, "gradle_wrapper") as gradle,
        ):
            build_android.main([])

        args_list = _scons_call_args(run_scons)
        # 4 editor + 8 template (4 archs × 2 targets) = 12 scons invocations.
        assert len(args_list) == 12
        # Two gradle tasks: generateGodotEditor + generateGodotTemplates.
        gradle_tasks = [c.args[1] for c in gradle.call_args_list]
        assert gradle_tasks == ["generateGodotEditor", "generateGodotTemplates"]


# ---------------------------------------------------------------------------
# build_macos
# ---------------------------------------------------------------------------


class TestBuildMacos:
    def test_classical_builds_two_archs_and_lipos(self, base_env, tmp_path):
        base_env.setenv("MONO", "0")
        godot = tmp_path / "godot"
        (godot / "bin").mkdir(parents=True)
        # Stage outputs that lipo would have produced inputs for.
        for name in (
            "godot.macos.editor.x86_64",
            "godot.macos.editor.arm64",
            "godot.macos.template_debug.x86_64",
            "godot.macos.template_debug.arm64",
            "godot.macos.template_release.x86_64",
            "godot.macos.template_release.arm64",
        ):
            (godot / "bin" / name).write_text("b")

        with (
            mock.patch.object(
                build_macos.common,
                "swiftly_install",
                return_value=tmp_path / "toolchains" / "6.2.1",
            ),
            mock.patch.object(
                build_macos.common, "setup_godot_source", return_value=godot
            ),
            mock.patch.object(
                build_macos.common, "run_scons", return_value=0
            ) as run_scons,
            mock.patch("scripts.in_container.build_macos.subprocess.run") as sp_run,
            mock.patch("scripts.in_container.build_macos._copy_bin_clean"),
            mock.patch(
                "scripts.in_container.build_macos._require_pre_installed_swift_toolchain"
            ),
        ):
            sp_run.return_value = subprocess.CompletedProcess([], 0)
            rc = build_macos.main([])

        assert rc == 0
        # 2 archs × 3 targets (editor, template_debug, template_release) = 6 scons calls.
        assert run_scons.call_count == 6
        for args in _scons_call_args(run_scons):
            assert "platform=macos" in args
            assert "osxcross_sdk=darwin25.1" in args
            assert any(a.startswith("SWIFT_FRONTEND=") for a in args)
        # lipo is called 3 times (one per target).
        lipo_calls = [c for c in sp_run.call_args_list if c.args[0][0] == "lipo"]
        assert len(lipo_calls) == 3

    def test_swift_version_env_override(self, base_env, tmp_path, monkeypatch):
        monkeypatch.setenv("SWIFT_VERSION", "6.3.0")
        base_env.setenv("MONO", "0")
        base_env.setenv("CLASSICAL", "0")
        godot = tmp_path / "godot"
        (godot / "bin").mkdir(parents=True)

        with (
            mock.patch.object(
                build_macos.common,
                "swiftly_install",
                return_value=tmp_path / "toolchains" / "6.3.0",
            ) as swiftly,
            mock.patch.object(
                build_macos.common, "setup_godot_source", return_value=godot
            ),
            mock.patch.object(build_macos.common, "run_scons", return_value=0),
            mock.patch(
                "scripts.in_container.build_macos._require_pre_installed_swift_toolchain"
            ),
        ):
            build_macos.main([])

        swiftly.assert_called_once_with("6.3.0")


# ---------------------------------------------------------------------------
# build_ios
# ---------------------------------------------------------------------------


class TestBuildIos:
    def test_classical_runs_arm64_device_plus_x86_64_simulator(
        self, base_env, tmp_path
    ):
        base_env.setenv("MONO", "0")
        godot = tmp_path / "godot"
        (godot / "bin").mkdir(parents=True)

        with (
            mock.patch.object(
                build_ios.common,
                "swiftly_install",
                return_value=tmp_path / "toolchains" / "6.2.1",
            ),
            mock.patch.object(
                build_ios.common, "setup_godot_source", return_value=godot
            ),
            mock.patch.object(
                build_ios.common, "run_scons", return_value=0
            ) as run_scons,
            mock.patch(
                "scripts.in_container.build_ios._require_pre_installed_swift_toolchain"
            ),
        ):
            rc = build_ios.main([])

        assert rc == 0
        args_list = _scons_call_args(run_scons)
        # 2 archs (arm64 device + x86_64 sim) × 2 targets = 4 invocations.
        assert len(args_list) == 4
        # arm64 calls carry the device SDK + APPLE_TARGET_ARM64.
        arm64 = [a for a in args_list if "arch=arm64" in a]
        assert len(arm64) == 2
        for args in arm64:
            assert any("iPhoneOS" in token for token in args)
            assert "APPLE_TOOLCHAIN_PATH=/root/ioscross/arm64" in args
        # x86_64 calls carry the simulator SDK.
        x86 = [a for a in args_list if "arch=x86_64" in a]
        assert len(x86) == 2
        for args in x86:
            assert "simulator=yes" in args
            assert "APPLE_TOOLCHAIN_PATH=/root/ioscross/x86_64" in args

    def test_swiftly_install_called_with_default_version(
        self, base_env, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("SWIFT_VERSION", raising=False)
        base_env.setenv("MONO", "0")
        base_env.setenv("CLASSICAL", "0")
        godot = tmp_path / "godot"
        (godot / "bin").mkdir(parents=True)

        with (
            mock.patch.object(
                build_ios.common,
                "swiftly_install",
                return_value=tmp_path / "toolchains" / "6.2.1",
            ) as swiftly,
            mock.patch.object(
                build_ios.common, "setup_godot_source", return_value=godot
            ),
            mock.patch.object(build_ios.common, "run_scons", return_value=0),
            mock.patch(
                "scripts.in_container.build_ios._require_pre_installed_swift_toolchain"
            ),
        ):
            build_ios.main([])

        # Default is 6.2.1, matching Xcode 26.1.1 SDK bundled Swift.
        swiftly.assert_called_once_with("6.2.1")


# ---------------------------------------------------------------------------
# Apple toolchain pre-flight check — clear actionable error when missing
# ---------------------------------------------------------------------------


class TestApplePreflightToolchainCheck:
    def test_macos_raises_actionable_error_when_default_toolchain_missing(
        self, monkeypatch
    ):
        # The pre-flight check uses the absolute /root/.local/share/swiftly/
        # path which won't exist outside a real godot-osx container — that's
        # exactly the failure mode the actionable error covers.
        monkeypatch.delenv("SWIFT_VERSION", raising=False)
        with pytest.raises(
            build_macos.common.InContainerBuildError, match="godot-osx image"
        ):
            build_macos._require_pre_installed_swift_toolchain("6.2.1")

    def test_macos_skips_check_for_overridden_swift_version(self, monkeypatch):
        # An operator override is their call — pre-flight defers to swiftly_install.
        build_macos._require_pre_installed_swift_toolchain("9.9.9")

    def test_ios_raises_actionable_error_when_default_toolchain_missing(
        self, monkeypatch
    ):
        monkeypatch.delenv("SWIFT_VERSION", raising=False)
        with pytest.raises(
            build_ios.common.InContainerBuildError, match="godot-ios image"
        ):
            build_ios._require_pre_installed_swift_toolchain("6.2.1")

    def test_ios_skips_check_for_overridden_swift_version(self):
        build_ios._require_pre_installed_swift_toolchain("9.9.9")


# ---------------------------------------------------------------------------
# build_web
# ---------------------------------------------------------------------------


class TestBuildWeb:
    def test_web_mono_skipped_with_info_when_mono_enabled(
        self, base_env, tmp_path, caplog
    ):
        # Upstream Godot does not support mono on web — see
        # upstream/godot/modules/mono/config.py line 14. The Mono pass must
        # log an INFO note and skip without invoking scons.
        base_env.setenv("CLASSICAL", "0")
        base_env.setenv("MONO", "1")
        godot = tmp_path / "godot"
        (godot / "bin").mkdir(parents=True)

        with (
            mock.patch.object(
                build_web.common, "setup_godot_source", return_value=godot
            ),
            mock.patch.object(build_web.common, "copy_mono_glue") as copy_glue,
            mock.patch.object(
                build_web.common, "run_scons", return_value=0
            ) as run_scons,
            mock.patch(
                "scripts.in_container.build_web._source_emsdk",
                side_effect=lambda e: e,
            ),
            caplog.at_level("INFO", logger="scripts.in_container.build_web"),
        ):
            rc = build_web.main([])

        assert rc == 0
        # No mono glue copy and no scons calls at all in MONO-only mode.
        copy_glue.assert_not_called()
        for args in _scons_call_args(run_scons):
            assert "module_mono_enabled=yes" not in args
            assert "module_dotnet_enabled=yes" not in args
        # An INFO line mentions the upstream constraint.
        info_messages = [
            r.getMessage() for r in caplog.records if r.levelname == "INFO"
        ]
        assert any(
            "not supported by Godot upstream" in msg and "platform=web" in msg
            for msg in info_messages
        )

    def test_web_classical_still_built_when_classical_enabled(self, base_env, tmp_path):
        base_env.setenv("CLASSICAL", "1")
        base_env.setenv("MONO", "0")
        godot = tmp_path / "godot"
        (godot / "bin").mkdir(parents=True)
        # Stage a dummy zip the bin glob would copy.
        (godot / "bin" / "godot.web.editor.wasm32.zip").write_text("z")

        with (
            mock.patch.object(
                build_web.common, "setup_godot_source", return_value=godot
            ),
            mock.patch.object(
                build_web.common, "run_scons", return_value=0
            ) as run_scons,
            mock.patch(
                "scripts.in_container.build_web._run_parallel_jobs"
            ) as parallel_jobs,
            mock.patch(
                "scripts.in_container.build_web._source_emsdk",
                side_effect=lambda e: e,
            ),
        ):
            rc = build_web.main([])

        assert rc == 0
        # The editor scons call is the only direct one; parallel jobs handle the rest.
        editor_calls = [
            args for args in _scons_call_args(run_scons) if "target=editor" in args
        ]
        assert len(editor_calls) == 1
        assert "use_closure_compiler=yes" in editor_calls[0]
        # Two parallel-job batches (threaded + nothreads).
        assert parallel_jobs.call_count == 2
