"""Host-side release packaging.

This module owns the post-build packaging step for ``build-godot.py release``:
turning the per-platform ``out/<plat>/<arch>/{tools,templates,...}/`` artifacts
into the editor zips, ``templates/`` staging, ``.tpz`` export-template bundles,
and ``SHA512-SUMS.txt`` files that get uploaded to a GitHub Release.

Notes on platform-specific edge cases:

1. **iOS / macOS xcframework template path** — the source lives in the
   ``upstream/godot`` submodule. The current upstream layout renames
   ``ios_xcode`` to ``apple_embedded_xcode``; we accept either directory
   name. When the source is missing (e.g. a tag without the path, or the
   operator hasn't checked out ``upstream/godot``) we log a clear WARNING
   and skip iOS/macOS app templating instead of hard-erroring — other
   platforms still publish.

2. **Web mono templates missing** — the Web build module does not produce
   mono web templates today. If the source files are absent we log INFO and
   skip web mono in the .tpz rather than failing the whole packaging step.

3. **Empty per-arch artifact dirs** — when the Windows arm64 mono pass
   fails in a half-built run we used to zip an empty source dir and upload
   a 210-byte stub. Each source dir is now checked for at least one
   expected artifact pattern for the platform/arch before zipping; a
   missing/incomplete arch is skipped with a WARNING and the other archs
   proceed normally. Applied consistently across every platform.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def package_release(
    *,
    basedir: Path,
    godot_version: str,
    godot_version_status: str,
    build_cfg: dict | None = None,
    upstream_godot_dir: Path | None = None,
    dry_run: bool = False,
) -> int:
    """Package every available platform artifact under ``basedir`` for release.

    Parameters
    ----------
    basedir
        ``build-godot-and-templates/`` — directory holding ``out/``,
        ``releases/``, ``deps/``, etc.
    godot_version
        Engine version, e.g. ``"4.7"``.
    godot_version_status
        Pre-release status tail, e.g. ``"dev1"``. The binaries version is
        ``<godot_version>-<godot_version_status>``; the templates version is
        ``<godot_version>.<godot_version_status>``.
    build_cfg
        Reserved for future use (mono/classical selection knobs); accepted
        and ignored today so callers can thread the full ``[build]`` matrix
        without churn.
    upstream_godot_dir
        Path to the checked-out Godot source (``upstream/godot/``). When
        ``None`` we default to ``<basedir>/../upstream/godot``. Used to copy
        the ``macos_tools.app`` / ``macos_template.app`` / iOS xcode template
        directories from the submodule.
    dry_run
        Log every action but write nothing.

    Returns
    -------
    int
        ``0`` on success. Non-zero only on a hard error that prevents any
        platform from being packaged (e.g. unwritable ``releases/`` dir).
        Missing per-platform artifacts are skipped with WARNING/INFO, not
        hard-errored, so a partial matrix still publishes.
    """
    del build_cfg  # accepted for forward compatibility; unused today.

    # Single source of truth — ``<godot_version>.<godot_version_status>`` (e.g.
    # ``4.7.beta``) comes from upstream/godot/version.py and drives BOTH the
    # published filename pattern AND ``version.txt`` inside the .tpz. Godot
    # looks templates up by ``version.txt`` at install time, so the install
    # path on the user's machine matches the engine binary's reported version
    # exactly. Diverging filenames from the engine version was the historical
    # source of "templates installed at 4.7.dev1 but engine reports 4.7.beta"
    # bugs — this layout makes the mismatch impossible.
    binaries_version = f"{godot_version}.{godot_version_status}"
    templates_version = binaries_version
    godot_basename = f"Godot_v{binaries_version}"

    out_dir = basedir / "out"
    release_dir = basedir / "releases" / binaries_version
    release_dir_mono = release_dir / "mono"
    tmp_dir = basedir / "tmp"
    templates_dir = tmp_dir / "templates"
    templates_dir_mono = tmp_dir / "mono" / "templates"

    if upstream_godot_dir is None:
        upstream_godot_dir = (basedir.parent / "upstream" / "godot").resolve()

    logger.info("Packager basedir=%s", basedir)
    logger.info("Packager binaries_version=%s", binaries_version)
    logger.info("Packager templates_version=%s", templates_version)
    logger.info("Packager release_dir=%s", release_dir)
    logger.info("Packager upstream_godot_dir=%s", upstream_godot_dir)

    if dry_run:
        logger.info("[dry-run] Would prepare release directories and stage artifacts.")
        return 0

    # Reset the release and templates staging dirs so a re-run is deterministic.
    for d in (release_dir, release_dir_mono, templates_dir, templates_dir_mono):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)

    # --- Classical ---
    _package_linux_classical(out_dir, release_dir, templates_dir, godot_basename)
    _package_windows_classical(out_dir, release_dir, templates_dir, godot_basename)
    _package_macos_classical(
        out_dir, release_dir, templates_dir, godot_basename, upstream_godot_dir
    )
    _package_web_classical(out_dir, release_dir, templates_dir, godot_basename)
    _package_android_classical(
        out_dir, release_dir, templates_dir, godot_basename, templates_version
    )
    _package_ios_classical(basedir, out_dir, templates_dir, upstream_godot_dir)

    _create_tpz_bundle(
        templates_dir,
        release_dir / f"{godot_basename}_export_templates.tpz",
        templates_version,
    )
    _generate_sha512sums(release_dir, recurse_mono=False)

    # --- Mono ---
    _package_linux_mono(out_dir, release_dir_mono, templates_dir_mono, godot_basename)
    _package_windows_mono(out_dir, release_dir_mono, templates_dir_mono, godot_basename)
    _package_macos_mono(
        out_dir,
        release_dir_mono,
        templates_dir_mono,
        godot_basename,
        upstream_godot_dir,
    )
    _package_web_mono(out_dir, templates_dir_mono)
    _package_android_mono(
        out_dir, release_dir_mono, templates_dir_mono, godot_basename, templates_version
    )
    _package_ios_mono(basedir, out_dir, templates_dir_mono, upstream_godot_dir)

    _create_tpz_bundle(
        templates_dir_mono,
        release_dir_mono / f"{godot_basename}_mono_export_templates.tpz",
        f"{templates_version}.mono",
    )
    _generate_sha512sums(release_dir_mono, recurse_mono=False)

    logger.info("Packaging complete: %s", release_dir)
    return 0


# ---------------------------------------------------------------------------
# Per-platform descriptors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ArchSpec:
    """Per-arch source/destination descriptor for a platform-flavor pair."""

    arch: str
    source_dir: Path
    artifact_patterns: tuple[str, ...]


_LINUX_ARCHS = ("x86_64", "x86_32", "arm64", "arm32")
_WINDOWS_ARCHS = ("x86_64", "x86_32", "arm64")


# ---------------------------------------------------------------------------
# Linux
# ---------------------------------------------------------------------------


def _package_linux_classical(
    out_dir: Path,
    release_dir: Path,
    templates_dir: Path,
    godot_basename: str,
) -> None:
    logger.info("Packaging Linux (classical)...")
    for arch in _LINUX_ARCHS:
        tools_dir = out_dir / "linux" / arch / "tools"
        templates_src = out_dir / "linux" / arch / "templates"

        # Editor
        editor_bin = tools_dir / f"godot.linuxbsd.editor.{arch}"
        if _have_real_artifacts(tools_dir, (f"godot.linuxbsd.editor.{arch}",)):
            zip_name = f"{godot_basename}_linux.{arch}.zip"
            _zip_single_file(
                editor_bin,
                release_dir / zip_name,
                arcname=f"{godot_basename}_linux.{arch}",
            )
        else:
            logger.warning(
                "Skipping Linux editor for arch '%s': %s missing or empty.",
                arch,
                tools_dir,
            )

        # Templates (release + debug)
        if _have_real_artifacts(
            templates_src,
            (
                f"godot.linuxbsd.template_release.{arch}",
                f"godot.linuxbsd.template_debug.{arch}",
            ),
            require_all=False,
        ):
            for variant in ("release", "debug"):
                src = templates_src / f"godot.linuxbsd.template_{variant}.{arch}"
                dest = templates_dir / f"linux_{variant}.{arch}"
                if src.is_file():
                    shutil.copy2(src, dest)
                else:
                    logger.warning(
                        "Linux template %s missing for arch '%s'.", variant, arch
                    )
        else:
            logger.warning(
                "Skipping Linux templates for arch '%s': %s missing or empty.",
                arch,
                templates_src,
            )


def _package_linux_mono(
    out_dir: Path,
    release_dir_mono: Path,
    templates_dir_mono: Path,
    godot_basename: str,
) -> None:
    logger.info("Packaging Linux (mono)...")
    for arch in _LINUX_ARCHS:
        tools_dir = out_dir / "linux" / arch / "tools-mono"
        templates_src = out_dir / "linux" / arch / "templates-mono"

        # Editor: bundle binary + GodotSharp/ into a dir, then zip the dir.
        editor_bin = tools_dir / f"godot.linuxbsd.editor.{arch}.mono"
        sharp_dir = tools_dir / "GodotSharp"
        if (
            _have_real_artifacts(tools_dir, (f"godot.linuxbsd.editor.{arch}.mono",))
            and sharp_dir.is_dir()
        ):
            binbasename = f"{godot_basename}_mono_linux"
            stage_root = release_dir_mono / f"{binbasename}_{arch}"
            stage_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(editor_bin, stage_root / f"{binbasename}.{arch}")
            shutil.copytree(sharp_dir, stage_root / "GodotSharp", dirs_exist_ok=True)
            _zip_directory(
                stage_root,
                release_dir_mono / f"{binbasename}_{arch}.zip",
                arcname_root=stage_root.name,
            )
            shutil.rmtree(stage_root)
        else:
            logger.warning(
                "Skipping Linux mono editor for arch '%s': %s missing or empty.",
                arch,
                tools_dir,
            )

        # Templates
        if _have_real_artifacts(
            templates_src,
            (
                f"godot.linuxbsd.template_release.{arch}.mono",
                f"godot.linuxbsd.template_debug.{arch}.mono",
            ),
            require_all=False,
        ):
            for variant in ("release", "debug"):
                src = templates_src / f"godot.linuxbsd.template_{variant}.{arch}.mono"
                dest = templates_dir_mono / f"linux_{variant}.{arch}"
                if src.is_file():
                    shutil.copy2(src, dest)
        else:
            logger.warning(
                "Skipping Linux mono templates for arch '%s': %s missing or empty.",
                arch,
                templates_src,
            )


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


def _package_windows_classical(
    out_dir: Path,
    release_dir: Path,
    templates_dir: Path,
    godot_basename: str,
) -> None:
    logger.info("Packaging Windows (classical)...")
    for arch in _WINDOWS_ARCHS:
        tools_dir = out_dir / "windows" / arch / "tools"
        templates_src = out_dir / "windows" / arch / "templates"

        editor_exe = tools_dir / f"godot.windows.editor.{arch}.exe"
        console_exe = tools_dir / f"godot.windows.editor.{arch}.console.exe"
        if (
            _have_real_artifacts(
                tools_dir,
                (
                    f"godot.windows.editor.{arch}.exe",
                    f"godot.windows.editor.{arch}.console.exe",
                ),
                require_all=False,
            )
            and editor_exe.is_file()
        ):
            zip_name = f"{godot_basename}_win{arch}.exe.zip"
            # Bash zipped both editor and console exe into one archive.
            files: list[tuple[Path, str]] = [
                (editor_exe, f"{godot_basename}_win{arch}.exe")
            ]
            if console_exe.is_file():
                files.append((console_exe, f"{godot_basename}_win{arch}_console.exe"))
            _zip_files(files, release_dir / zip_name)
        else:
            logger.warning(
                "Skipping Windows editor for arch '%s': %s missing or empty.",
                arch,
                tools_dir,
            )

        # Templates: release + debug, each with optional console variant.
        if _have_real_artifacts(
            templates_src,
            (
                f"godot.windows.template_release.{arch}.exe",
                f"godot.windows.template_debug.{arch}.exe",
            ),
            require_all=False,
        ):
            for variant in ("release", "debug"):
                for suffix, out_suffix in (
                    ("", f"{arch}"),
                    (".console", f"{arch}_console"),
                ):
                    src = (
                        templates_src
                        / f"godot.windows.template_{variant}.{arch}{suffix}.exe"
                    )
                    if src.is_file():
                        dest = templates_dir / f"windows_{variant}_{out_suffix}.exe"
                        shutil.copy2(src, dest)
        else:
            logger.warning(
                "Skipping Windows templates for arch '%s': %s missing or empty.",
                arch,
                templates_src,
            )


def _package_windows_mono(
    out_dir: Path,
    release_dir_mono: Path,
    templates_dir_mono: Path,
    godot_basename: str,
) -> None:
    logger.info("Packaging Windows (mono)...")
    for arch in _WINDOWS_ARCHS:
        tools_dir = out_dir / "windows" / arch / "tools-mono"
        templates_src = out_dir / "windows" / arch / "templates-mono"

        editor_exe = tools_dir / f"godot.windows.editor.{arch}.mono.exe"
        console_exe = tools_dir / f"godot.windows.editor.{arch}.mono.console.exe"
        sharp_dir = tools_dir / "GodotSharp"
        # Gap 3: a half-built arm64 mono run left this dir empty / partial. Skip
        # rather than zip a 210-byte stub.
        if (
            _have_real_artifacts(tools_dir, (f"godot.windows.editor.{arch}.mono.exe",))
            and editor_exe.is_file()
            and sharp_dir.is_dir()
        ):
            binname = f"{godot_basename}_mono_win{arch}"
            stage_root = release_dir_mono / binname
            stage_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(editor_exe, stage_root / f"{binname}.exe")
            if console_exe.is_file():
                shutil.copy2(console_exe, stage_root / f"{binname}_console.exe")
            shutil.copytree(sharp_dir, stage_root / "GodotSharp", dirs_exist_ok=True)
            _zip_directory(
                stage_root,
                release_dir_mono / f"{binname}.zip",
                arcname_root=stage_root.name,
            )
            shutil.rmtree(stage_root)
        else:
            logger.warning(
                "Skipping Windows mono editor for arch '%s': %s missing or empty "
                "(half-built arm64 mono is the common cause; safe to skip).",
                arch,
                tools_dir,
            )

        if _have_real_artifacts(
            templates_src,
            (
                f"godot.windows.template_release.{arch}.mono.exe",
                f"godot.windows.template_debug.{arch}.mono.exe",
            ),
            require_all=False,
        ):
            for variant in ("release", "debug"):
                for suffix, out_suffix in (
                    ("", f"{arch}"),
                    (".console", f"{arch}_console"),
                ):
                    src = (
                        templates_src
                        / f"godot.windows.template_{variant}.{arch}.mono{suffix}.exe"
                    )
                    if src.is_file():
                        dest = (
                            templates_dir_mono / f"windows_{variant}_{out_suffix}.exe"
                        )
                        shutil.copy2(src, dest)
        else:
            logger.warning(
                "Skipping Windows mono templates for arch '%s': %s missing or empty.",
                arch,
                templates_src,
            )


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------


def _find_macos_template_dir(upstream_godot_dir: Path, name: str) -> Path | None:
    """Return ``<upstream>/misc/dist/<name>`` if present, else ``None``."""
    candidate = upstream_godot_dir / "misc" / "dist" / name
    if candidate.is_dir():
        return candidate
    return None


def _package_macos_classical(
    out_dir: Path,
    release_dir: Path,
    templates_dir: Path,
    godot_basename: str,
    upstream_godot_dir: Path,
) -> None:
    logger.info("Packaging macOS (classical)...")
    tools_dir = out_dir / "macos" / "tools"
    templates_src = out_dir / "macos" / "templates"

    # Editor (.app bundle)
    macos_tools = _find_macos_template_dir(upstream_godot_dir, "macos_tools.app")
    editor_bin = tools_dir / "godot.macos.editor.universal"
    if macos_tools is not None and _have_real_artifacts(
        tools_dir, ("godot.macos.editor.universal",)
    ):
        binname = f"{godot_basename}_macos.universal"
        stage = templates_dir.parent / "Godot.app"
        if stage.exists():
            shutil.rmtree(stage)
        shutil.copytree(macos_tools, stage)
        (stage / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
        dest_bin = stage / "Contents" / "MacOS" / "Godot"
        shutil.copy2(editor_bin, dest_bin)
        dest_bin.chmod(dest_bin.stat().st_mode | 0o111)
        _zip_directory(stage, release_dir / f"{binname}.zip", arcname_root="Godot.app")
        shutil.rmtree(stage)
    else:
        logger.warning(
            "Skipping macOS editor: macos_tools.app template (under %s) or "
            "editor binary (%s) missing.",
            upstream_godot_dir / "misc" / "dist",
            tools_dir,
        )

    # Templates (.app bundle)
    macos_template = _find_macos_template_dir(upstream_godot_dir, "macos_template.app")
    if macos_template is not None and _have_real_artifacts(
        templates_src,
        (
            "godot.macos.template_release.universal",
            "godot.macos.template_debug.universal",
        ),
        require_all=False,
    ):
        stage = templates_dir.parent / "macos_template.app"
        if stage.exists():
            shutil.rmtree(stage)
        shutil.copytree(macos_template, stage)
        (stage / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
        for variant in ("release", "debug"):
            src = templates_src / f"godot.macos.template_{variant}.universal"
            if src.is_file():
                dest = stage / "Contents" / "MacOS" / f"godot_macos_{variant}.universal"
                shutil.copy2(src, dest)
                dest.chmod(dest.stat().st_mode | 0o111)
        _zip_directory(
            stage, templates_dir / "macos.zip", arcname_root="macos_template.app"
        )
        shutil.rmtree(stage)
    else:
        logger.warning(
            "Skipping macOS template bundle: macos_template.app (under %s) or "
            "template binaries (%s) missing.",
            upstream_godot_dir / "misc" / "dist",
            templates_src,
        )


def _package_macos_mono(
    out_dir: Path,
    release_dir_mono: Path,
    templates_dir_mono: Path,
    godot_basename: str,
    upstream_godot_dir: Path,
) -> None:
    logger.info("Packaging macOS (mono)...")
    tools_dir = out_dir / "macos" / "tools-mono"
    templates_src = out_dir / "macos" / "templates-mono"

    macos_tools = _find_macos_template_dir(upstream_godot_dir, "macos_tools.app")
    editor_bin = tools_dir / "godot.macos.editor.universal.mono"
    sharp_dir = tools_dir / "GodotSharp"
    if (
        macos_tools is not None
        and _have_real_artifacts(tools_dir, ("godot.macos.editor.universal.mono",))
        and sharp_dir.is_dir()
    ):
        binname = f"{godot_basename}_mono_macos.universal"
        stage = templates_dir_mono.parent / "Godot_mono.app"
        if stage.exists():
            shutil.rmtree(stage)
        shutil.copytree(macos_tools, stage)
        (stage / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
        (stage / "Contents" / "Resources").mkdir(parents=True, exist_ok=True)
        dest_bin = stage / "Contents" / "MacOS" / "Godot"
        shutil.copy2(editor_bin, dest_bin)
        dest_bin.chmod(dest_bin.stat().st_mode | 0o111)
        shutil.copytree(
            sharp_dir,
            stage / "Contents" / "Resources" / "GodotSharp",
            dirs_exist_ok=True,
        )
        _zip_directory(
            stage, release_dir_mono / f"{binname}.zip", arcname_root="Godot_mono.app"
        )
        shutil.rmtree(stage)
    else:
        logger.warning(
            "Skipping macOS mono editor: macos_tools.app template or mono "
            "editor binary missing under %s / %s.",
            upstream_godot_dir / "misc" / "dist",
            tools_dir,
        )

    macos_template = _find_macos_template_dir(upstream_godot_dir, "macos_template.app")
    if macos_template is not None and _have_real_artifacts(
        templates_src,
        (
            "godot.macos.template_release.universal.mono",
            "godot.macos.template_debug.universal.mono",
        ),
        require_all=False,
    ):
        stage = templates_dir_mono.parent / "macos_template.app"
        if stage.exists():
            shutil.rmtree(stage)
        shutil.copytree(macos_template, stage)
        (stage / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
        (stage / "Contents" / "Resources").mkdir(parents=True, exist_ok=True)
        for variant in ("release", "debug"):
            src = templates_src / f"godot.macos.template_{variant}.universal.mono"
            if src.is_file():
                dest = stage / "Contents" / "MacOS" / f"godot_macos_{variant}.universal"
                shutil.copy2(src, dest)
                dest.chmod(dest.stat().st_mode | 0o111)
        _zip_directory(
            stage,
            templates_dir_mono / "macos.zip",
            arcname_root="macos_template.app",
        )
        shutil.rmtree(stage)
    else:
        logger.warning(
            "Skipping macOS mono templates: macos_template.app template or "
            "binaries missing under %s / %s.",
            upstream_godot_dir / "misc" / "dist",
            templates_src,
        )


# ---------------------------------------------------------------------------
# Web
# ---------------------------------------------------------------------------


def _package_web_classical(
    out_dir: Path,
    release_dir: Path,
    templates_dir: Path,
    godot_basename: str,
) -> None:
    logger.info("Packaging Web (classical)...")
    editor_zip = out_dir / "web" / "tools" / "godot.web.editor.wasm32.zip"
    templates_src = out_dir / "web" / "templates"

    if editor_zip.is_file():
        shutil.copy2(editor_zip, release_dir / f"{godot_basename}_web_editor.zip")
    else:
        logger.warning("Skipping Web editor: %s missing.", editor_zip)

    # Templates: 4 variants per (release, debug).
    variant_specs = (
        ("godot.web.template_{v}.wasm32.zip", "web_{v}.zip"),
        ("godot.web.template_{v}.wasm32.nothreads.zip", "web_nothreads_{v}.zip"),
        ("godot.web.template_{v}.wasm32.dlink.zip", "web_dlink_{v}.zip"),
        (
            "godot.web.template_{v}.wasm32.nothreads.dlink.zip",
            "web_dlink_nothreads_{v}.zip",
        ),
    )
    for variant in ("release", "debug"):
        for src_tmpl, dest_tmpl in variant_specs:
            src = templates_src / src_tmpl.format(v=variant)
            if src.is_file():
                shutil.copy2(src, templates_dir / dest_tmpl.format(v=variant))
            else:
                logger.info("Web template %s missing; skipping.", src.name)


def _package_web_mono(out_dir: Path, templates_dir_mono: Path) -> None:
    """Package web mono templates if present.

    Note: the Web build module currently doesn't produce Mono web templates.
    When they're absent we INFO-log and skip rather than failing the run.
    """
    logger.info("Packaging Web (mono)...")
    templates_src = out_dir / "web" / "templates-mono"
    if not templates_src.is_dir():
        logger.info(
            "Web mono templates not built (%s missing); skipping web mono in .tpz.",
            templates_src,
        )
        return

    found_any = False
    for variant in ("release", "debug"):
        src = templates_src / f"godot.web.template_{variant}.wasm32.mono.zip"
        if src.is_file():
            shutil.copy2(src, templates_dir_mono / f"web_{variant}.zip")
            found_any = True
    if not found_any:
        logger.info(
            "Web mono templates not built (no godot.web.template_*.wasm32.mono.zip "
            "under %s); skipping web mono in .tpz.",
            templates_src,
        )


# ---------------------------------------------------------------------------
# Android
# ---------------------------------------------------------------------------


def _package_android_classical(
    out_dir: Path,
    release_dir: Path,
    templates_dir: Path,
    godot_basename: str,
    templates_version: str,
) -> None:
    logger.info("Packaging Android (classical)...")
    templates_src = out_dir / "android" / "templates"
    tools_src = out_dir / "android" / "tools"

    lib = templates_src / "godot-lib.template_release.aar"
    if lib.is_file():
        shutil.copy2(
            lib,
            release_dir / f"godot-lib.{templates_version}.template_release.aar",
        )
    else:
        logger.warning("Skipping Android .aar: %s missing.", lib)

    # Editor APKs / AAB (best-effort)
    for editor in (
        "android_editor.apk",
        "android_editor_horizonos.apk",
        "android_editor.aab",
    ):
        src = tools_src / editor
        if src.is_file():
            shutil.copy2(src, release_dir / f"{godot_basename}_{editor}")
        else:
            logger.info("Android editor artifact %s missing; skipping.", editor)

    # Templates: copy any .apk + android_source.zip
    if templates_src.is_dir():
        for apk in sorted(templates_src.glob("*.apk")):
            shutil.copy2(apk, templates_dir / apk.name)
        src_zip = templates_src / "android_source.zip"
        if src_zip.is_file():
            shutil.copy2(src_zip, templates_dir / "android_source.zip")


def _package_android_mono(
    out_dir: Path,
    release_dir_mono: Path,
    templates_dir_mono: Path,
    godot_basename: str,
    templates_version: str,
) -> None:
    logger.info("Packaging Android (mono)...")
    del godot_basename  # not used for mono android — the AAR is renamed below.
    templates_src = out_dir / "android" / "templates-mono"

    lib = templates_src / "godot-lib.template_release.aar"
    if lib.is_file():
        shutil.copy2(
            lib,
            release_dir_mono
            / f"godot-lib.{templates_version}.mono.template_release.aar",
        )
    else:
        logger.info("Skipping Android mono .aar: %s missing.", lib)

    if templates_src.is_dir():
        for apk in sorted(templates_src.glob("*.apk")):
            shutil.copy2(apk, templates_dir_mono / apk.name)
        src_zip = templates_src / "android_source.zip"
        if src_zip.is_file():
            shutil.copy2(src_zip, templates_dir_mono / "android_source.zip")


# ---------------------------------------------------------------------------
# iOS
# ---------------------------------------------------------------------------


# Library mapping: relative path under the xcode template dir → source .a name
# under out/ios/templates(-mono)/.
_IOS_LIB_MAP: dict[str, str] = {
    "libgodot.ios.simulator.a": "libgodot.ios.release.xcframework/ios-arm64_x86_64-simulator/libgodot.a",
    "libgodot.ios.debug.simulator.a": "libgodot.ios.debug.xcframework/ios-arm64_x86_64-simulator/libgodot.a",
    "libgodot.ios.a": "libgodot.ios.release.xcframework/ios-arm64/libgodot.a",
    "libgodot.ios.debug.a": "libgodot.ios.debug.xcframework/ios-arm64/libgodot.a",
}


def _find_ios_xcode_dir(upstream_godot_dir: Path) -> Path | None:
    """Locate the iOS xcode template dir under the upstream submodule.

    Accepts both the legacy ``ios_xcode`` name and the current upstream
    ``apple_embedded_xcode`` name (4.7 upstream renamed the directory). Returns
    ``None`` if neither is present.
    """
    for name in ("ios_xcode", "apple_embedded_xcode"):
        candidate = upstream_godot_dir / "misc" / "dist" / name
        if candidate.is_dir():
            return candidate
    return None


def _package_ios_common(
    basedir: Path,
    out_dir: Path,
    templates_dest: Path,
    upstream_godot_dir: Path,
    *,
    mono: bool,
) -> None:
    flavor = "mono" if mono else "classical"
    logger.info("Packaging iOS (%s)...", flavor)
    xcode_src = _find_ios_xcode_dir(upstream_godot_dir)
    if xcode_src is None:
        logger.warning(
            "Skipping iOS (%s): no ios_xcode/apple_embedded_xcode template found "
            "under %s. (Have you initialised the upstream/godot submodule?)",
            flavor,
            upstream_godot_dir / "misc" / "dist",
        )
        return

    out_ios_templates = out_dir / "ios" / ("templates-mono" if mono else "templates")
    if not _have_real_artifacts(
        out_ios_templates,
        tuple(_IOS_LIB_MAP.keys()),
        require_all=False,
    ):
        logger.warning(
            "Skipping iOS (%s): no xcframework libgodot.ios*.a found under %s.",
            flavor,
            out_ios_templates,
        )
        return

    stage = basedir / "tmp" / ("ios_xcode_mono" if mono else "ios_xcode")
    if stage.exists():
        shutil.rmtree(stage)
    shutil.copytree(xcode_src, stage)

    copied_any = False
    for src_name, rel_dest in _IOS_LIB_MAP.items():
        src = out_ios_templates / src_name
        dest = stage / rel_dest
        if src.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            copied_any = True
        else:
            logger.info("iOS lib %s missing; skipping that slot.", src)

    # MoltenVK — best-effort, the deps folder isn't always present locally.
    moltenvk = basedir / "deps" / "moltenvk" / "MoltenVK" / "MoltenVK.xcframework"
    if moltenvk.is_dir():
        dest = stage / "MoltenVK.xcframework"
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(moltenvk, dest)
        # Drop macos / tvos slots that aren't needed in the iOS bundle.
        for child in dest.iterdir():
            if child.name.startswith(("macos", "tvos")):
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
    else:
        logger.info("MoltenVK.xcframework not found at %s; not bundling.", moltenvk)

    if not copied_any:
        logger.warning(
            "iOS (%s): xcode template found but no .a libs were stageable; "
            "skipping ios.zip.",
            flavor,
        )
        shutil.rmtree(stage)
        return

    _zip_directory_contents(stage, templates_dest / "ios.zip")
    shutil.rmtree(stage)


def _package_ios_classical(
    basedir: Path,
    out_dir: Path,
    templates_dir: Path,
    upstream_godot_dir: Path,
) -> None:
    _package_ios_common(basedir, out_dir, templates_dir, upstream_godot_dir, mono=False)


def _package_ios_mono(
    basedir: Path,
    out_dir: Path,
    templates_dir_mono: Path,
    upstream_godot_dir: Path,
) -> None:
    _package_ios_common(
        basedir, out_dir, templates_dir_mono, upstream_godot_dir, mono=True
    )


# ---------------------------------------------------------------------------
# TPZ + SHA512 helpers
# ---------------------------------------------------------------------------


def _create_tpz_bundle(
    staging_dir: Path,
    output_path: Path,
    version_text: str,
) -> None:
    """Zip ``staging_dir`` into ``output_path`` with a top-level ``version.txt``.

    The .tpz convention is a zipfile whose entries are rooted at
    ``templates/`` and which contains a ``templates/version.txt`` file at
    the top of the staging dir.
    """
    if not staging_dir.is_dir():
        logger.warning(
            "TPZ source dir %s missing; not creating %s.", staging_dir, output_path
        )
        return

    # Always (re)write version.txt to reflect the just-built release.
    (staging_dir / "version.txt").write_text(version_text + "\n", encoding="utf-8")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    arcname_root = staging_dir.name  # entries land under "templates/...".
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(staging_dir.rglob("*")):
            if path.is_file():
                rel = path.relative_to(staging_dir)
                zf.write(path, arcname=f"{arcname_root}/{rel.as_posix()}")
    logger.info("Created TPZ %s", output_path)


def _generate_sha512sums(directory: Path, *, recurse_mono: bool) -> None:
    """Write a ``SHA512-SUMS.txt`` alongside every release asset in *directory*.

    Hash every release file beginning with ``g`` / ``G`` in the directory
    (editor zips, .tpz, godot-lib.aar). The ``mono/`` subdir gets its own
    SHA512-SUMS.txt independently when packaging_mono runs after.
    """
    del recurse_mono
    if not directory.is_dir():
        return
    sums_path = directory / "SHA512-SUMS.txt"
    lines: list[str] = []
    for entry in sorted(directory.iterdir()):
        if not entry.is_file():
            continue
        if entry.name == "SHA512-SUMS.txt":
            continue
        # Bash matched files starting with G or g (Godot_v..., godot-lib...).
        if not entry.name[:1].lower().startswith("g"):
            continue
        digest = _sha512_of(entry)
        lines.append(f"{digest}  {entry.name}")
    sums_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    logger.info("Wrote %d sha512 line(s) to %s", len(lines), sums_path)


def _sha512_of(path: Path) -> str:
    h = hashlib.sha512()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Generic zip helpers
# ---------------------------------------------------------------------------


def _zip_single_file(src: Path, output_path: Path, *, arcname: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.write(src, arcname=arcname)


def _zip_files(files: Iterable[tuple[Path, str]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for src, arcname in files:
            zf.write(src, arcname=arcname)


def _zip_directory(directory: Path, output_path: Path, *, arcname_root: str) -> None:
    """Zip *directory* into *output_path*, prefixing every entry with *arcname_root*."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                rel = path.relative_to(directory)
                zf.write(path, arcname=f"{arcname_root}/{rel.as_posix()}")


def _zip_directory_contents(directory: Path, output_path: Path) -> None:
    """Zip the *contents* of *directory* (no top-level prefix)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                rel = path.relative_to(directory)
                zf.write(path, arcname=rel.as_posix())


# ---------------------------------------------------------------------------
# Empty-artifact detection (Gap 3)
# ---------------------------------------------------------------------------


def _have_real_artifacts(
    directory: Path,
    patterns: tuple[str, ...],
    *,
    require_all: bool = False,
) -> bool:
    """Return ``True`` when *directory* exists, is non-empty, and contains
    artifact files matching *patterns*.

    *patterns* are exact basenames (Path.name match) OR glob patterns; we try
    both. ``require_all`` selects between "every pattern matches" (True) and
    "any pattern matches" (False, the default). Gap 3 needs at least one real
    artifact for the arch, which the default covers.
    """
    if not directory.is_dir():
        return False

    # Empty dir is never enough.
    entries = list(directory.iterdir())
    if not entries:
        return False

    matched = 0
    for pat in patterns:
        # Try exact basename first (cheapest), then glob.
        exact = directory / pat
        if exact.is_file() and exact.stat().st_size > 0:
            matched += 1
            continue
        for candidate in directory.glob(pat):
            if candidate.is_file() and candidate.stat().st_size > 0:
                matched += 1
                break

    if require_all:
        return matched == len(patterns)
    return matched >= 1
