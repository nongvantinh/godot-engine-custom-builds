"""Tests for scripts/packager.py — host-side release packaging.

Stage a fake ``out/<plat>/<arch>/{tools,templates,...}/`` tree under tmp_path,
optionally include a fake ``upstream/godot/misc/dist/`` tree for the macOS/iOS
templates, then assert that :func:`scripts.packager.package_release` produces
the expected editor zips, ``.tpz`` bundles, ``version.txt`` entries and
``SHA512-SUMS.txt`` files. The gap-coverage tests at the bottom exercise
edge cases that previously caused release-publishing bugs (iOS xcode path
relocation, missing web mono templates, empty/half-built arch dirs).
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path

import pytest

from scripts import packager


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


_GODOT_VERSION = "4.7"
_STATUS = "dev1"
# Single source of truth — filenames and templates_version both use the
# dot-joined `<version>.<status>` form (e.g. "4.7.dev1") so the install path
# Godot derives from `version.txt` matches the engine binary's reported
# version exactly. The hyphen-joined `4.7-dev1` form is no longer produced.
_BINARIES_VERSION = f"{_GODOT_VERSION}.{_STATUS}"
_TEMPLATES_VERSION = _BINARIES_VERSION
_GODOT_BASENAME = f"Godot_v{_BINARIES_VERSION}"


def _write(path: Path, content: str = "binary-data") -> Path:
    """Create *path* (and parents) with non-empty content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _stage_linux(
    out_dir: Path, *, archs=("x86_64", "x86_32", "arm64", "arm32"), mono: bool = False
) -> None:
    for arch in archs:
        tools = out_dir / "linux" / arch / ("tools-mono" if mono else "tools")
        templates = (
            out_dir / "linux" / arch / ("templates-mono" if mono else "templates")
        )
        if mono:
            _write(tools / f"godot.linuxbsd.editor.{arch}.mono")
            (tools / "GodotSharp").mkdir(parents=True, exist_ok=True)
            _write(tools / "GodotSharp" / "GodotSharp.dll", "sharp")
            for v in ("release", "debug"):
                _write(templates / f"godot.linuxbsd.template_{v}.{arch}.mono")
        else:
            _write(tools / f"godot.linuxbsd.editor.{arch}")
            for v in ("release", "debug"):
                _write(templates / f"godot.linuxbsd.template_{v}.{arch}")


def _stage_windows(
    out_dir: Path, *, archs=("x86_64", "x86_32", "arm64"), mono: bool = False
) -> None:
    for arch in archs:
        tools = out_dir / "windows" / arch / ("tools-mono" if mono else "tools")
        templates = (
            out_dir / "windows" / arch / ("templates-mono" if mono else "templates")
        )
        if mono:
            _write(tools / f"godot.windows.editor.{arch}.mono.exe")
            _write(tools / f"godot.windows.editor.{arch}.mono.console.exe")
            (tools / "GodotSharp").mkdir(parents=True, exist_ok=True)
            _write(tools / "GodotSharp" / "GodotSharp.dll", "sharp")
            for v in ("release", "debug"):
                _write(templates / f"godot.windows.template_{v}.{arch}.mono.exe")
                _write(
                    templates / f"godot.windows.template_{v}.{arch}.mono.console.exe"
                )
        else:
            _write(tools / f"godot.windows.editor.{arch}.exe")
            _write(tools / f"godot.windows.editor.{arch}.console.exe")
            for v in ("release", "debug"):
                _write(templates / f"godot.windows.template_{v}.{arch}.exe")
                _write(templates / f"godot.windows.template_{v}.{arch}.console.exe")


def _stage_macos(out_dir: Path, *, mono: bool = False) -> None:
    tools = out_dir / "macos" / ("tools-mono" if mono else "tools")
    templates = out_dir / "macos" / ("templates-mono" if mono else "templates")
    suffix = ".mono" if mono else ""
    _write(tools / f"godot.macos.editor.universal{suffix}")
    for v in ("release", "debug"):
        _write(templates / f"godot.macos.template_{v}.universal{suffix}")
    if mono:
        (tools / "GodotSharp").mkdir(parents=True, exist_ok=True)
        _write(tools / "GodotSharp" / "GodotSharp.dll", "sharp")


def _stage_web(out_dir: Path) -> None:
    web_tools = out_dir / "web" / "tools"
    web_templates = out_dir / "web" / "templates"
    _write(web_tools / "godot.web.editor.wasm32.zip", "editor")
    for v in ("release", "debug"):
        _write(web_templates / f"godot.web.template_{v}.wasm32.zip")
        _write(web_templates / f"godot.web.template_{v}.wasm32.nothreads.zip")
        _write(web_templates / f"godot.web.template_{v}.wasm32.dlink.zip")
        _write(web_templates / f"godot.web.template_{v}.wasm32.nothreads.dlink.zip")


def _stage_android(out_dir: Path, *, mono: bool = False) -> None:
    templates = out_dir / "android" / ("templates-mono" if mono else "templates")
    _write(templates / "godot-lib.template_release.aar")
    _write(templates / "android_release.apk")
    _write(templates / "android_source.zip")
    if not mono:
        tools = out_dir / "android" / "tools"
        _write(tools / "android_editor.apk")


def _stage_ios(out_dir: Path, *, mono: bool = False) -> None:
    templates = out_dir / "ios" / ("templates-mono" if mono else "templates")
    for name in (
        "libgodot.ios.simulator.a",
        "libgodot.ios.debug.simulator.a",
        "libgodot.ios.a",
        "libgodot.ios.debug.a",
    ):
        _write(templates / name)


def _stage_upstream_apple(
    upstream_godot: Path, *, ios_dir_name: str = "apple_embedded_xcode"
) -> None:
    """Stage the macos/ios template app dirs under upstream/godot/misc/dist/."""
    dist = upstream_godot / "misc" / "dist"
    # macos_tools.app + macos_template.app — minimal Contents tree.
    for app in ("macos_tools.app", "macos_template.app"):
        contents = dist / app / "Contents"
        contents.mkdir(parents=True, exist_ok=True)
        _write(contents / "Info.plist", "<plist/>")
    # iOS xcode template (legacy name OR new apple_embedded_xcode).
    xcode = dist / ios_dir_name
    for slot in (
        "libgodot.ios.release.xcframework/ios-arm64_x86_64-simulator",
        "libgodot.ios.debug.xcframework/ios-arm64_x86_64-simulator",
        "libgodot.ios.release.xcframework/ios-arm64",
        "libgodot.ios.debug.xcframework/ios-arm64",
    ):
        (xcode / slot).mkdir(parents=True, exist_ok=True)
    _write(xcode / "Info.plist", "<plist/>")


@pytest.fixture
def basedir(tmp_path: Path) -> Path:
    """Return the fake build-godot-and-templates/ basedir for one test."""
    bd = tmp_path / "build-godot-and-templates"
    bd.mkdir()
    return bd


@pytest.fixture
def upstream_godot(tmp_path: Path) -> Path:
    """Return the fake upstream/godot dir (sibling to basedir)."""
    p = tmp_path / "upstream" / "godot"
    p.mkdir(parents=True)
    return p


# ---------------------------------------------------------------------------
# Full-matrix happy-path tests
# ---------------------------------------------------------------------------


class TestFullMatrix:
    def test_packages_full_matrix_produces_expected_artifacts(
        self, basedir, upstream_godot
    ):
        out_dir = basedir / "out"
        _stage_linux(out_dir, mono=False)
        _stage_linux(out_dir, mono=True)
        _stage_windows(out_dir, mono=False)
        _stage_windows(out_dir, mono=True)
        _stage_macos(out_dir, mono=False)
        _stage_macos(out_dir, mono=True)
        _stage_web(out_dir)
        _stage_android(out_dir, mono=False)
        _stage_android(out_dir, mono=True)
        _stage_ios(out_dir, mono=False)
        _stage_ios(out_dir, mono=True)
        _stage_upstream_apple(upstream_godot)

        rc = packager.package_release(
            basedir=basedir,
            godot_version=_GODOT_VERSION,
            godot_version_status=_STATUS,
            upstream_godot_dir=upstream_godot,
        )

        assert rc == 0
        release_dir = basedir / "releases" / _BINARIES_VERSION
        # Linux editor zips (classical)
        for arch in ("x86_64", "x86_32", "arm64", "arm32"):
            assert (release_dir / f"{_GODOT_BASENAME}_linux.{arch}.zip").is_file()
        # Windows editor zips (classical)
        for arch in ("x86_64", "x86_32", "arm64"):
            assert (release_dir / f"{_GODOT_BASENAME}_win{arch}.exe.zip").is_file()
        # macOS editor zip
        assert (release_dir / f"{_GODOT_BASENAME}_macos.universal.zip").is_file()
        # Web editor zip
        assert (release_dir / f"{_GODOT_BASENAME}_web_editor.zip").is_file()
        # Android .aar at release root (classical)
        assert (
            release_dir / f"godot-lib.{_TEMPLATES_VERSION}.template_release.aar"
        ).is_file()

        # Mono editor zips
        mono_dir = release_dir / "mono"
        for arch in ("x86_64", "x86_32", "arm64", "arm32"):
            assert (mono_dir / f"{_GODOT_BASENAME}_mono_linux_{arch}.zip").is_file()
        for arch in ("x86_64", "x86_32", "arm64"):
            assert (mono_dir / f"{_GODOT_BASENAME}_mono_win{arch}.zip").is_file()
        assert (mono_dir / f"{_GODOT_BASENAME}_mono_macos.universal.zip").is_file()

        # .tpz bundles (both flavors)
        tpz = release_dir / f"{_GODOT_BASENAME}_export_templates.tpz"
        tpz_mono = mono_dir / f"{_GODOT_BASENAME}_mono_export_templates.tpz"
        assert tpz.is_file()
        assert tpz_mono.is_file()

        # SHA512-SUMS.txt in both
        assert (release_dir / "SHA512-SUMS.txt").is_file()
        assert (mono_dir / "SHA512-SUMS.txt").is_file()

    def test_tpz_contains_version_txt_with_correct_text(self, basedir, upstream_godot):
        out_dir = basedir / "out"
        _stage_linux(out_dir, mono=False, archs=("x86_64",))
        _stage_linux(out_dir, mono=True, archs=("x86_64",))
        _stage_upstream_apple(upstream_godot)

        packager.package_release(
            basedir=basedir,
            godot_version=_GODOT_VERSION,
            godot_version_status=_STATUS,
            upstream_godot_dir=upstream_godot,
        )

        release_dir = basedir / "releases" / _BINARIES_VERSION
        tpz = release_dir / f"{_GODOT_BASENAME}_export_templates.tpz"
        with zipfile.ZipFile(tpz) as zf:
            names = zf.namelist()
            # The .tpz must contain a templates/version.txt entry.
            assert "templates/version.txt" in names
            assert (
                zf.read("templates/version.txt").decode().strip() == _TEMPLATES_VERSION
            )

        tpz_mono = release_dir / "mono" / f"{_GODOT_BASENAME}_mono_export_templates.tpz"
        with zipfile.ZipFile(tpz_mono) as zf:
            assert "templates/version.txt" in zf.namelist()
            text = zf.read("templates/version.txt").decode().strip()
            assert text == f"{_TEMPLATES_VERSION}.mono"

    def test_sha512_sums_hash_release_files(self, basedir, upstream_godot):
        out_dir = basedir / "out"
        _stage_linux(out_dir, mono=False, archs=("x86_64",))
        _stage_linux(out_dir, mono=True, archs=("x86_64",))
        _stage_upstream_apple(upstream_godot)

        packager.package_release(
            basedir=basedir,
            godot_version=_GODOT_VERSION,
            godot_version_status=_STATUS,
            upstream_godot_dir=upstream_godot,
        )

        sums = (
            basedir / "releases" / _BINARIES_VERSION / "SHA512-SUMS.txt"
        ).read_text()
        assert f"{_GODOT_BASENAME}_linux.x86_64.zip" in sums
        # Each non-empty line is "<128-hex>  <filename>"
        for line in sums.splitlines():
            digest, name = line.split("  ", 1)
            assert len(digest) == 128
            assert name.startswith(("G", "g"))

    def test_dry_run_writes_nothing(self, basedir, upstream_godot):
        out_dir = basedir / "out"
        _stage_linux(out_dir, mono=False, archs=("x86_64",))
        _stage_upstream_apple(upstream_godot)

        rc = packager.package_release(
            basedir=basedir,
            godot_version=_GODOT_VERSION,
            godot_version_status=_STATUS,
            upstream_godot_dir=upstream_godot,
            dry_run=True,
        )

        assert rc == 0
        assert not (basedir / "releases").exists()
        assert not (basedir / "tmp").exists()


# ---------------------------------------------------------------------------
# Gap 1 — iOS / macOS xcode template now lives in upstream/godot/, not git/
# ---------------------------------------------------------------------------


class TestIosTemplateFromUpstreamSubmodule:
    def test_ios_zip_built_when_apple_embedded_xcode_present(
        self, basedir, upstream_godot
    ):
        _stage_ios(basedir / "out", mono=False)
        _stage_upstream_apple(upstream_godot, ios_dir_name="apple_embedded_xcode")

        packager.package_release(
            basedir=basedir,
            godot_version=_GODOT_VERSION,
            godot_version_status=_STATUS,
            upstream_godot_dir=upstream_godot,
        )

        tpz = (
            basedir
            / "releases"
            / _BINARIES_VERSION
            / f"{_GODOT_BASENAME}_export_templates.tpz"
        )
        with zipfile.ZipFile(tpz) as zf:
            assert "templates/ios.zip" in zf.namelist()

    def test_ios_zip_built_when_legacy_ios_xcode_present(self, basedir, upstream_godot):
        _stage_ios(basedir / "out", mono=False)
        _stage_upstream_apple(upstream_godot, ios_dir_name="ios_xcode")

        packager.package_release(
            basedir=basedir,
            godot_version=_GODOT_VERSION,
            godot_version_status=_STATUS,
            upstream_godot_dir=upstream_godot,
        )

        tpz = (
            basedir
            / "releases"
            / _BINARIES_VERSION
            / f"{_GODOT_BASENAME}_export_templates.tpz"
        )
        with zipfile.ZipFile(tpz) as zf:
            assert "templates/ios.zip" in zf.namelist()

    def test_ios_skipped_with_warning_when_upstream_missing(
        self, basedir, upstream_godot, caplog
    ):
        # iOS libs exist but the xcode template dir does NOT — should skip
        # cleanly without raising, and other platforms still publish.
        out_dir = basedir / "out"
        _stage_ios(out_dir, mono=False)
        _stage_linux(out_dir, mono=False, archs=("x86_64",))
        # upstream_godot intentionally has no misc/dist

        with caplog.at_level(logging.WARNING, logger="scripts.packager"):
            rc = packager.package_release(
                basedir=basedir,
                godot_version=_GODOT_VERSION,
                godot_version_status=_STATUS,
                upstream_godot_dir=upstream_godot,
            )

        assert rc == 0
        assert any("Skipping iOS" in r.message for r in caplog.records)
        # Linux editor zip is still produced — other platforms not blocked.
        assert (
            basedir
            / "releases"
            / _BINARIES_VERSION
            / f"{_GODOT_BASENAME}_linux.x86_64.zip"
        ).is_file()
        # No ios.zip ended up in the tpz.
        tpz = (
            basedir
            / "releases"
            / _BINARIES_VERSION
            / f"{_GODOT_BASENAME}_export_templates.tpz"
        )
        with zipfile.ZipFile(tpz) as zf:
            assert "templates/ios.zip" not in zf.namelist()


class TestMacosTemplateFromUpstreamSubmodule:
    def test_macos_app_skipped_with_warning_when_template_apps_missing(
        self, basedir, upstream_godot, caplog
    ):
        out_dir = basedir / "out"
        _stage_macos(out_dir, mono=False)
        _stage_linux(out_dir, mono=False, archs=("x86_64",))
        # upstream_godot dir exists but has no misc/dist/macos_*.app

        with caplog.at_level(logging.WARNING, logger="scripts.packager"):
            rc = packager.package_release(
                basedir=basedir,
                godot_version=_GODOT_VERSION,
                godot_version_status=_STATUS,
                upstream_godot_dir=upstream_godot,
            )

        assert rc == 0
        # macOS skipped — Linux editor still produced.
        release_dir = basedir / "releases" / _BINARIES_VERSION
        assert not (release_dir / f"{_GODOT_BASENAME}_macos.universal.zip").is_file()
        assert (release_dir / f"{_GODOT_BASENAME}_linux.x86_64.zip").is_file()
        assert any("Skipping macOS" in r.message for r in caplog.records)

    def test_macos_app_built_when_template_apps_present(self, basedir, upstream_godot):
        _stage_macos(basedir / "out", mono=False)
        _stage_upstream_apple(upstream_godot)

        packager.package_release(
            basedir=basedir,
            godot_version=_GODOT_VERSION,
            godot_version_status=_STATUS,
            upstream_godot_dir=upstream_godot,
        )

        release_dir = basedir / "releases" / _BINARIES_VERSION
        editor_zip = release_dir / f"{_GODOT_BASENAME}_macos.universal.zip"
        assert editor_zip.is_file()
        with zipfile.ZipFile(editor_zip) as zf:
            names = zf.namelist()
            # The editor binary lives under Godot.app/Contents/MacOS/Godot.
            assert any(n.endswith("Godot.app/Contents/MacOS/Godot") for n in names)


# ---------------------------------------------------------------------------
# Gap 2 — Web mono templates may not exist; must skip cleanly with INFO.
# ---------------------------------------------------------------------------


class TestWebMonoOptional:
    def test_web_mono_missing_skipped_with_info(self, basedir, upstream_godot, caplog):
        # Stage classical web only — no out/web/templates-mono/.
        out_dir = basedir / "out"
        _stage_web(out_dir)
        _stage_upstream_apple(upstream_godot)

        with caplog.at_level(logging.INFO, logger="scripts.packager"):
            rc = packager.package_release(
                basedir=basedir,
                godot_version=_GODOT_VERSION,
                godot_version_status=_STATUS,
                upstream_godot_dir=upstream_godot,
            )

        assert rc == 0
        # No empty mono web zip should be in the mono templates tpz.
        tpz_mono = (
            basedir
            / "releases"
            / _BINARIES_VERSION
            / "mono"
            / f"{_GODOT_BASENAME}_mono_export_templates.tpz"
        )
        with zipfile.ZipFile(tpz_mono) as zf:
            assert "templates/web_release.zip" not in zf.namelist()
            assert "templates/web_debug.zip" not in zf.namelist()

        # INFO message logged.
        assert any("Web mono templates not built" in r.message for r in caplog.records)

    def test_web_mono_zips_present_when_built(self, basedir, upstream_godot):
        out_dir = basedir / "out"
        _stage_web(out_dir)
        # Stage web mono explicitly.
        for v in ("release", "debug"):
            _write(
                out_dir
                / "web"
                / "templates-mono"
                / f"godot.web.template_{v}.wasm32.mono.zip"
            )
        _stage_upstream_apple(upstream_godot)

        packager.package_release(
            basedir=basedir,
            godot_version=_GODOT_VERSION,
            godot_version_status=_STATUS,
            upstream_godot_dir=upstream_godot,
        )

        tpz_mono = (
            basedir
            / "releases"
            / _BINARIES_VERSION
            / "mono"
            / f"{_GODOT_BASENAME}_mono_export_templates.tpz"
        )
        with zipfile.ZipFile(tpz_mono) as zf:
            names = zf.namelist()
            assert "templates/web_release.zip" in names
            assert "templates/web_debug.zip" in names


# ---------------------------------------------------------------------------
# Gap 3 — empty / half-built arch dirs must be skipped, not zipped as stubs.
# ---------------------------------------------------------------------------


class TestEmptyArchDirSkipped:
    def test_windows_arm64_mono_empty_templates_skipped_with_warning(
        self, basedir, upstream_godot, caplog
    ):
        # Stage windows mono for x86_64 (real) + arm64 (empty templates-mono).
        out_dir = basedir / "out"
        _stage_windows(out_dir, mono=True, archs=("x86_64",))
        # arm64: tools-mono exists but templates-mono is empty (half-built).
        (out_dir / "windows" / "arm64" / "tools-mono").mkdir(parents=True)
        (out_dir / "windows" / "arm64" / "templates-mono").mkdir(parents=True)
        _stage_upstream_apple(upstream_godot)

        with caplog.at_level(logging.WARNING, logger="scripts.packager"):
            rc = packager.package_release(
                basedir=basedir,
                godot_version=_GODOT_VERSION,
                godot_version_status=_STATUS,
                upstream_godot_dir=upstream_godot,
            )

        assert rc == 0
        # arm64 mono editor zip should NOT exist (this was the 210-byte stub bug).
        mono_dir = basedir / "releases" / _BINARIES_VERSION / "mono"
        assert not (mono_dir / f"{_GODOT_BASENAME}_mono_winarm64.zip").is_file()
        # x86_64 mono editor zip SHOULD exist (other archs proceed normally).
        assert (mono_dir / f"{_GODOT_BASENAME}_mono_winx86_64.zip").is_file()
        # Warning logged for arm64.
        warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("arm64" in m for m in warnings)

    def test_linux_arch_with_empty_tools_dir_skipped(
        self, basedir, upstream_godot, caplog
    ):
        out_dir = basedir / "out"
        _stage_linux(out_dir, mono=False, archs=("x86_64",))
        # Empty tools dir for arm64 — simulates an aborted build.
        (out_dir / "linux" / "arm64" / "tools").mkdir(parents=True)
        _stage_upstream_apple(upstream_godot)

        with caplog.at_level(logging.WARNING, logger="scripts.packager"):
            packager.package_release(
                basedir=basedir,
                godot_version=_GODOT_VERSION,
                godot_version_status=_STATUS,
                upstream_godot_dir=upstream_godot,
            )

        release_dir = basedir / "releases" / _BINARIES_VERSION
        assert (release_dir / f"{_GODOT_BASENAME}_linux.x86_64.zip").is_file()
        assert not (release_dir / f"{_GODOT_BASENAME}_linux.arm64.zip").is_file()

    def test_zero_byte_artifact_is_not_considered_real(self, basedir, upstream_godot):
        out_dir = basedir / "out"
        # Tools dir exists with a zero-byte stub binary — must be treated as not built.
        tools = out_dir / "linux" / "x86_64" / "tools"
        tools.mkdir(parents=True)
        (tools / "godot.linuxbsd.editor.x86_64").write_bytes(b"")
        _stage_upstream_apple(upstream_godot)

        packager.package_release(
            basedir=basedir,
            godot_version=_GODOT_VERSION,
            godot_version_status=_STATUS,
            upstream_godot_dir=upstream_godot,
        )

        release_dir = basedir / "releases" / _BINARIES_VERSION
        assert not (release_dir / f"{_GODOT_BASENAME}_linux.x86_64.zip").is_file()


# ---------------------------------------------------------------------------
# _have_real_artifacts helper
# ---------------------------------------------------------------------------


class TestHaveRealArtifacts:
    def test_returns_false_for_missing_dir(self, tmp_path):
        assert packager._have_real_artifacts(tmp_path / "nope", ("anything",)) is False

    def test_returns_false_for_empty_dir(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert packager._have_real_artifacts(empty, ("godot.*",)) is False

    def test_returns_false_when_no_pattern_matches(self, tmp_path):
        d = tmp_path / "d"
        d.mkdir()
        (d / "stray.txt").write_text("x")
        assert (
            packager._have_real_artifacts(d, ("godot.linuxbsd.editor.x86_64",)) is False
        )

    def test_returns_true_for_exact_basename_match(self, tmp_path):
        d = tmp_path / "d"
        d.mkdir()
        (d / "godot.linuxbsd.editor.x86_64").write_text("bin")
        assert (
            packager._have_real_artifacts(d, ("godot.linuxbsd.editor.x86_64",)) is True
        )

    def test_returns_true_for_glob_match(self, tmp_path):
        d = tmp_path / "d"
        d.mkdir()
        (d / "godot.windows.template_release.x86_64.exe").write_text("bin")
        assert (
            packager._have_real_artifacts(d, ("godot.windows.template_*.x86_64.exe",))
            is True
        )

    def test_require_all_requires_every_pattern(self, tmp_path):
        d = tmp_path / "d"
        d.mkdir()
        (d / "a").write_text("x")
        assert packager._have_real_artifacts(d, ("a", "b"), require_all=False) is True
        assert packager._have_real_artifacts(d, ("a", "b"), require_all=True) is False


# ---------------------------------------------------------------------------
# Path defaulting
# ---------------------------------------------------------------------------


class TestUpstreamGodotDirDefault:
    def test_default_resolves_to_basedir_parent_upstream_godot(
        self, basedir, upstream_godot, caplog
    ):
        # The basedir fixture is at <tmp>/build-godot-and-templates and the
        # upstream_godot fixture is at <tmp>/upstream/godot — exactly the
        # layout the packager defaults to (basedir.parent / upstream / godot).
        _stage_macos(basedir / "out", mono=False)
        _stage_upstream_apple(upstream_godot)

        # No upstream_godot_dir override — let the default kick in.
        with caplog.at_level(logging.INFO, logger="scripts.packager"):
            rc = packager.package_release(
                basedir=basedir,
                godot_version=_GODOT_VERSION,
                godot_version_status=_STATUS,
            )

        assert rc == 0
        editor_zip = (
            basedir
            / "releases"
            / _BINARIES_VERSION
            / f"{_GODOT_BASENAME}_macos.universal.zip"
        )
        assert editor_zip.is_file()
