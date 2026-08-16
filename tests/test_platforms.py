"""Tests for the platform registry (``scripts.platforms``) and the host
orchestrator behaviour derived from it.

The registry is the single source of truth for platform facts; these tests pin
both the registry contents and — crucially — that the derived image-name and
container-mount wiring reproduces the exact behaviour the release pipeline
depends on (a regression net for the platforms that cannot be built here).
"""

from __future__ import annotations

from pathlib import Path

from scripts import host_orchestrator, platforms


class TestRegistry:
    def test_supported_names(self):
        assert platforms.names() == frozenset(
            {"linux", "windows", "android", "web", "macos", "ios"}
        )

    def test_release_order_linux_android_windows_then_apple_and_web(self):
        order = platforms.release_order()
        assert order == ["linux", "android", "windows", "macos", "ios", "web"]

    def test_only_linux_skips_docker(self):
        assert platforms.docker_required_names() == platforms.names() - {"linux"}

    def test_only_linux_is_arch_scoped(self):
        assert platforms.arch_scoped_names() == frozenset({"linux"})

    def test_macos_image_basename_is_osx(self):
        # Historical quirk preserved: macOS image is godot-osx, not godot-macos.
        assert platforms.get("macos").image_basename == "godot-osx"
        assert (
            platforms.image_ref("macos", "ghcr.io", "u", "4.8")
            == "ghcr.io/u/godot-osx:4.8"
        )

    def test_image_ref_default_pattern(self):
        assert (
            platforms.image_ref("linux", "ghcr.io", "nongvantinh", "4.8")
            == "ghcr.io/nongvantinh/godot-linux:4.8"
        )

    def test_windows_carries_steam_env_and_winrt_dep(self):
        win = platforms.get("windows")
        assert win.extra_env == (("STEAM", "0"),)
        assert "winrt" in win.deps


class TestResolveImageNames:
    def test_matches_config_naming_for_all_platforms(self):
        images = host_orchestrator._resolve_image_names("ghcr.io", "u", "4.8")
        assert images == {
            "linux": "ghcr.io/u/godot-linux:4.8",
            "android": "ghcr.io/u/godot-android:4.8",
            "windows": "ghcr.io/u/godot-windows:4.8",
            "macos": "ghcr.io/u/godot-osx:4.8",
            "ios": "ghcr.io/u/godot-ios:4.8",
            "web": "ghcr.io/u/godot-web:4.8",
        }


class TestIterPlatformsMounts:
    """Regression net: the registry-derived mounts must exactly reproduce the
    previously hand-written docker -v wiring for every platform."""

    def test_exact_yield_sequence(self):
        basedir = Path("/bd")
        images = host_orchestrator._resolve_image_names("ghcr.io", "u", "4.8")
        got = list(host_orchestrator._iter_platforms(basedir=basedir, images=images))

        def mounts(name, *deps):
            m = ["-v", f"/bd/out/{name}:/root/out"]
            for dep in deps:
                m += ["-v", f"/bd/deps/{dep}:/root/{dep}"]
            return m

        expected = [
            ("linux", images["linux"], mounts("linux", "accesskit"), {}),
            ("android", images["android"], mounts("android", "swappy", "keystore"), {}),
            (
                "windows",
                images["windows"],
                mounts("windows", "angle", "accesskit", "winrt"),
                {"STEAM": "0"},
            ),
            (
                "macos",
                images["macos"],
                mounts("macos", "moltenvk", "angle", "accesskit"),
                {},
            ),
            ("ios", images["ios"], mounts("ios"), {}),
            ("web", images["web"], mounts("web"), {}),
        ]
        assert got == expected

    def test_apple_targets_always_yielded(self):
        images = host_orchestrator._resolve_image_names("ghcr.io", "u", "4.8")
        yielded = [
            name
            for name, _img, _m, _e in host_orchestrator._iter_platforms(
                basedir=Path("/bd"), images=images
            )
        ]
        assert "macos" in yielded and "ios" in yielded
