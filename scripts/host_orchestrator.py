"""Host-side docker orchestrator — Python entry point for matrix builds.

The host-side dispatcher for the all-Python build pipeline, imported directly
by :mod:`scripts.orchestrator` and the ``build-godot.py release`` subcommand.

Responsibilities:

  1. Dependency download — AccessKit, MoltenVK, ANGLE, Swappy into
     ``${basedir}/deps/<name>/``. Mesa NIR is installed inside the Windows
     container by ``install_d3d12_sdk_windows.py --mingw_prefix`` (no
     host-side fetch).
  2. Godot source preparation — uses the local ``upstream/godot`` submodule,
     warns on branch mismatch, extracts version + status, generates
     ``godot-${version}.tar.gz`` via ``misc/scripts/make_tarball.sh`` and
     moves it next to ``basedir``.
  3. Image pull — pulls 6 platform images from GHCR resolved as
     ``${registry}/${username}/godot-<plat>:${container_version}``.
  4. Resumability gate — skips per-platform ``docker run`` when
     ``out/<plat>/`` already contains regular files at depth ≥ 2. Mono-glue
     gate is depth ≥ 1 under ``mono-glue/``.
  5. Mono glue — single ``docker run godot-linux:<ver> python3 -m
     scripts.in_container.build_mono_glue`` into ``mono-glue/``.
  6. Per-platform ``docker run`` — Linux, Android, Windows, Web, and Apple
     (macOS + iOS). All six platforms always run; if the Apple toolchain is
     not set up the in-container build emits an actionable error and the run
     fails. Each platform mounts the source tarball, mono-glue and the
     platform-specific deps. Env vars threaded into the container:
     ``BUILD_NAME``, ``GODOT_VERSION_STATUS``, ``NUM_CORES``, ``CLASSICAL``,
     ``MONO``. Per-run logs under ``logs/<run-id>/``; each scons arch/target
     pass writes its own log file inside the container log mount.
  7. ``--clean-release`` / ``--cleanup`` — exposed as the ``mode`` kwarg.

The cosmetic ``chown`` of build outputs back to the invoking user is NOT done
here. It is run by the caller (``build-godot.py``) as the very last step of the
``release`` flow — after packaging and publishing — so that an interruption
during the long recursive chown can never cost the expensive build/publish
work. See :func:`_chown_outputs`.

Resilience features:

  * **Partial-state retry on dep download.** The dir-marker
    ``deps/<name>/`` is not treated as proof-of-success — we check the
    *extracted* artefact (a known file/dir inside each dep) and re-extract
    if missing. ``.7z``/``.zip`` re-extraction is automatic.
  * **Curl flakiness retry.** A transient
    :class:`urllib.error.URLError` does not abort the whole run; we retry
    up to 4 times with exponential backoff and a 60 s socket timeout (same
    wrapper as ``install_d3d12_sdk_windows.py`` in the build container).
  * **``chown`` failure-safe.** The chown runs *last* (after publish) and
    isolates every path: :class:`OSError` on any single file is counted and
    skipped, never aborting the rest, and the function never raises. Combined
    with run-anywhere resumability, an interruption during the chown costs
    nothing.
  * **Tarball directory.** ``${basedir}/../upstream/`` is used as the
    intermediate tarball location and the move into ``${basedir}/`` is a
    :func:`shutil.move` that raises a clear Python error if either path is
    missing.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import textwrap
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from scripts import build_logs, platforms
from scripts.console import section, spinner, summary_table

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — dependency download URLs (one per ``download_*`` helper below)
# ---------------------------------------------------------------------------

# Dependency versions are the ENGINE's single source of truth. Rather than
# hardcode (and inevitably drift from) each version, we read it at run time
# from the engine's own ``misc/scripts/install_*.py`` pins — see
# :func:`_engine_install_version`. This is the same principle the Windows
# Mesa/D3D12 path already follows by running ``install_d3d12_sdk_windows.py``
# in-container. Only MoltenVK has no engine installer, so it stays pinned here.
_MOLTENVK_URL = (
    "https://github.com/godotengine/moltenvk-osxcross/releases/download/"
    "vulkan-sdk-1.3.283.0-2/MoltenVK-all.tar"
)

# ANGLE per-arch bundle variants (arch/toolchain suffix is stable across
# versions; only the release tag moves, and that comes from the engine). The
# engine links arch-tagged lib names (``libANGLE.windows.<arch>.a``) so all
# variants can share the flat ``/root/angle`` dir — matching upstream
# godot-build-scripts. Windows x86 uses the gcc/mingw variant; arm64 uses the
# llvm variant (built with llvm-mingw).
_ANGLE_ARCH_VARIANTS: tuple[tuple[str, str], ...] = (
    ("windows_arm64.zip", "arm64-llvm"),
    ("windows_x86_64.zip", "x86_64-gcc"),
    ("windows_x86_32.zip", "x86_32-gcc"),
    ("macos_arm64.zip", "arm64-macos"),
    ("macos_x86_64.zip", "x86_64-macos"),
)

# The one file per Android ABI that ``platform/android/detect.py`` probes for
# (``detect_swappy``) and links as ``swappy_static``. The ABI list itself comes
# from the engine (``swappy_archs``), so an added ABI needs no edit here.
_SWAPPY_LIB = "libswappy_static.a"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


_Mode = Literal["build", "clean-release", "cleanup"]


def run_build(
    *,
    basedir: Path,
    registry: str,
    username: str,
    container_version: str,
    git_branch: str,
    godot_repo: str,
    upstream_godot_dir: Path,
    build_type: str,
    num_cores: int,
    platforms: list[str] | None = None,
    build_archs: list[str] | None = None,
    skip_download_containers: bool = False,
    force: bool = False,
    build_name: str = "",
    version_status_patch: str = "",
    run_logs_dir: Path | None = None,
    dry_run: bool = False,
    mode: _Mode = "build",
) -> int:
    """Run the host-side build dispatcher in-process.

    *godot_repo* is accepted for caller convenience but is not used by the
    build flow itself — the engine source is always the local
    ``upstream_godot_dir`` submodule.

    *platforms* scopes the run to a subset of the six supported platforms
    (``None`` = all). Only the scoped platforms' images are acquired and only
    their per-platform build passes run; packaging/publishing downstream still
    reflect the union of everything present in ``out/`` (resumability keeps
    previously built platforms), so a scoped run adds to a Release rather than
    replacing it. *build_archs* (``None`` = every arch the container supports)
    restricts the in-container arch matrix via the ``GODOT_BUILD_ARCHS`` env.

    Returns 0 on success, non-zero on a hard error.
    """
    del godot_repo  # accepted for caller convenience; build flow uses upstream_godot_dir.

    if mode == "clean-release":
        return _clean_release(basedir, dry_run=dry_run)
    if mode == "cleanup":
        return _cleanup(basedir, dry_run=dry_run)
    if mode != "build":
        logger.error("run_build: unknown mode '%s'.", mode)
        return 1

    logger.info("Building Godot engine and its templates")
    logger.info("  basedir=%s", basedir)
    logger.info("  registry=%s username=%s", registry, username)
    logger.info("  container_version=%s git_branch=%s", container_version, git_branch)
    logger.info("  build_type=%s num_cores=%s", build_type, num_cores)
    logger.info(
        "  skip_download_containers=%s",
        skip_download_containers,
    )

    images = _resolve_image_names(registry, username, container_version)

    # Normalize the platform scope (None = all six). Validate up front so a
    # typo fails loud instead of silently building nothing.
    scope: set[str] | None
    if platforms is None:
        scope = None
    else:
        scope = {p.strip().lower() for p in platforms if p.strip()}
        unknown = scope - set(images)
        if unknown:
            logger.error(
                "Unknown platform(s) %s. Supported: %s.",
                ", ".join(sorted(unknown)),
                ", ".join(sorted(images)),
            )
            return 1
    logger.info(
        "  platforms=%s build_archs=%s",
        "all" if scope is None else ",".join(sorted(scope)),
        "all" if not build_archs else ",".join(build_archs),
    )

    try:
        if not skip_download_containers:
            _pull_images(images, platforms=scope, dry_run=dry_run)
        else:
            logger.info("Skipping `docker pull` (skip_download_containers=True).")

        _download_deps(basedir, upstream_godot_dir, dry_run=dry_run)
        godot_version, godot_version_status = _prepare_source(
            basedir=basedir,
            upstream_godot_dir=upstream_godot_dir,
            git_branch=git_branch,
            version_status_patch=version_status_patch,
            dry_run=dry_run,
        )

        out_dir = basedir / "out"
        mono_glue_dir = basedir / "mono-glue"
        if run_logs_dir is None:
            run_logs_dir = build_logs.allocate_run_logs_dir(basedir, dry_run=dry_run)
        if not dry_run:
            out_dir.mkdir(parents=True, exist_ok=True)
            mono_glue_dir.mkdir(parents=True, exist_ok=True)

        build_classical, build_mono = _resolve_build_flags(build_type)
        common_env = {
            "BUILD_NAME": build_name,
            "GODOT_VERSION_STATUS": godot_version_status,
            "NUM_CORES": str(num_cores),
            "CLASSICAL": "1" if build_classical else "0",
            "MONO": "1" if build_mono else "0",
            "GODOT_LOG_DIR": "/root/logs",
        }
        # Restrict the in-container arch matrix when requested. Unset (empty)
        # means "every arch the container supports" — the historical default.
        if build_archs:
            common_env["GODOT_BUILD_ARCHS"] = ",".join(build_archs)
        tarball_path = basedir / f"godot-{godot_version}.tar.gz"

        # --- Mono glue ---
        if force and not dry_run and _mono_glue_has_artifacts(mono_glue_dir):
            logger.info("Force rebuild: clearing mono-glue at %s", mono_glue_dir)
            shutil.rmtree(mono_glue_dir, ignore_errors=True)
        if not force and _mono_glue_has_artifacts(mono_glue_dir):
            logger.info(
                "Skipping mono-glue: %s already populated. Delete it to force a rebuild.",
                mono_glue_dir,
            )
        else:
            # Mono glue is generated with the Linux image. When the run is
            # scoped to a non-Linux platform the Linux image was not pulled
            # above, so make sure it is available before the glue pass.
            if not skip_download_containers and (
                scope is not None and "linux" not in scope
            ):
                _ensure_image_available(images["linux"], "linux", dry_run=dry_run)
            _run_build_mono_glue(
                basedir=basedir,
                linux_image=images["linux"],
                tarball=tarball_path,
                mono_glue_dir=mono_glue_dir,
                run_logs_dir=run_logs_dir,
                env=common_env,
                dry_run=dry_run,
            )

        # --- Per-platform ---
        platform_results: list[tuple[str, str, str]] = []
        for plat, image, extra_mounts, extra_env in _iter_platforms(
            basedir=basedir,
            images=images,
        ):
            if scope is not None and plat not in scope:
                continue
            out_plat = out_dir / plat
            if force and not dry_run and _platform_has_artifacts(out_plat):
                logger.info("Force rebuild: clearing artifacts at %s", out_plat)
                shutil.rmtree(out_plat, ignore_errors=True)
            if not dry_run:
                out_plat.mkdir(parents=True, exist_ok=True)
            if not force and _platform_has_artifacts(out_plat):
                logger.info(
                    "Skipping %s: artifacts already present in %s. "
                    "Delete them to force a rebuild.",
                    plat,
                    out_plat,
                )
                platform_results.append((plat, "skipped", "-"))
                continue
            section(f"Building platform: {plat}", image)
            started = time.monotonic()
            _run_build_platform(
                name=plat,
                image=image,
                basedir=basedir,
                tarball=tarball_path,
                mono_glue_dir=mono_glue_dir,
                run_logs_dir=run_logs_dir,
                out_plat=out_plat,
                env={**common_env, **extra_env, "GODOT_BUILD_PLATFORM": plat},
                extra_mounts=extra_mounts,
                dry_run=dry_run,
            )
            elapsed = "-" if dry_run else f"{time.monotonic() - started:.1f}s"
            platform_results.append((plat, "dry-run" if dry_run else "ok", elapsed))

        summary_table(
            "Build matrix summary",
            ["Platform", "Status", "Elapsed"],
            platform_results,
            status_column=1,
        )

        # NOTE: the cosmetic chown of out/ + mono-glue back to the invoking
        # user is intentionally NOT done here. The caller runs it as the final
        # step of the release flow (after packaging + publishing) so an
        # interruption during the long recursive chown cannot cost the build.
        return 0

    except _HostOrchestratorError as exc:
        logger.error("%s", exc)
        return 1


# ---------------------------------------------------------------------------
# Internal exceptions
# ---------------------------------------------------------------------------


class _HostOrchestratorError(RuntimeError):
    """Raised on a hard error inside the host orchestrator flow."""


# ---------------------------------------------------------------------------
# Image refs + pull
# ---------------------------------------------------------------------------


def _resolve_image_names(
    registry: str, username: str, container_version: str
) -> dict[str, str]:
    """Resolve the container image ref for every platform in the registry.

    Derived from :mod:`scripts.platforms` (the single source of truth for
    image basenames) so the tags MUST match what ``containers --push``
    produces and what config.toml declares.
    """
    return {
        name: platforms.image_ref(name, registry, username, container_version)
        for name in platforms.release_order()
    }


def _image_present_locally(image: str) -> bool:
    """Return ``True`` if *image* already exists in the local Docker store."""
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _ensure_image_available(image: str, plat: str, *, dry_run: bool) -> None:
    """Make *image* available locally, pulling from the registry only if absent.

    A locally present image is used as-is (no pull) so an operator can build
    against a locally built or retagged image that is not published to the
    registry — the common case when preparing the first Release of a new
    version before the images have been pushed. Only a genuinely missing image
    triggers a ``docker pull``; a failed pull is a hard error.
    """
    if dry_run:
        logger.info("[dry-run] Would ensure image %s (%s) is available", image, plat)
        return
    if _image_present_locally(image):
        logger.info("Using local image %s (%s); skipping pull.", image, plat)
        return
    logger.info("Pulling image: %s", image)
    result = subprocess.run(["docker", "pull", image])
    if result.returncode != 0:
        raise _HostOrchestratorError(
            f"`docker pull {image}` exited with code {result.returncode}."
        )


def _pull_images(
    images: dict[str, str], *, platforms: set[str] | None = None, dry_run: bool
) -> None:
    logger.info("Fetching container images...")
    for plat, image in images.items():
        if platforms is not None and plat not in platforms:
            continue
        _ensure_image_available(image, plat, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Dependency download
# ---------------------------------------------------------------------------


def _engine_install_version(
    upstream_godot_dir: Path, script_name: str, var_name: str
) -> str:
    """Read a version pin from the engine's ``misc/scripts/<script_name>``.

    The engine's ``install_*.py`` scripts are the single source of truth for
    the exact dependency versions Godot expects (e.g. ``angle_version``,
    ``ac_version``, ``winrt_version``). We parse the literal ``<var> = "..."``
    assignment out of the script rather than maintain a parallel, drift-prone
    copy here. Fails loud if the script or the variable is missing so a
    structural upstream change surfaces immediately instead of silently
    falling back to a stale value.
    """
    script = upstream_godot_dir / "misc" / "scripts" / script_name
    if not script.is_file():
        raise _HostOrchestratorError(
            f"Engine dependency pin script not found: {script}. Cannot resolve "
            f"{var_name} — the Godot submodule may be missing or restructured."
        )
    text = script.read_text(encoding="utf-8")
    match = re.search(
        rf"^\s*{re.escape(var_name)}\s*=\s*[\"']([^\"']+)[\"']", text, re.MULTILINE
    )
    if not match:
        raise _HostOrchestratorError(
            f"Could not find `{var_name}` in {script}. The engine may have "
            f"renamed or restructured the pin; update _engine_install_version "
            f"callers to match."
        )
    return match.group(1)


def _engine_install_str_list(
    upstream_godot_dir: Path, script_name: str, var_name: str
) -> list[str]:
    """Read a list-of-strings pin (e.g. ``swappy_archs``) from the engine.

    Same single-source-of-truth contract as :func:`_engine_install_version`,
    for pins the engine expresses as a literal list. Fails loud when the
    assignment is absent or empty so an upstream restructure surfaces here
    instead of silently producing a short arch list.
    """
    script = upstream_godot_dir / "misc" / "scripts" / script_name
    if not script.is_file():
        raise _HostOrchestratorError(
            f"Engine dependency pin script not found: {script}. Cannot resolve "
            f"{var_name} — the Godot submodule may be missing or restructured."
        )
    text = script.read_text(encoding="utf-8")
    match = re.search(
        rf"^\s*{re.escape(var_name)}\s*=\s*\[(.*?)\]", text, re.MULTILINE | re.DOTALL
    )
    values = re.findall(r"[\"']([^\"']+)[\"']", match.group(1)) if match else []
    if not values:
        raise _HostOrchestratorError(
            f"Could not find a non-empty `{var_name}` list in {script}. The "
            f"engine may have renamed or restructured the pin; update "
            f"_engine_install_str_list callers to match."
        )
    return values


def _dep_marker_current(target: Path, version: str) -> bool:
    """Return True iff *target* was last populated for this exact *version*.

    A ``.dep-version`` marker makes the resumability cache version-aware: a
    bump to the engine's pinned version invalidates a stale local copy and
    forces a fresh download instead of silently reusing the old one.
    """
    marker = target / ".dep-version"
    return marker.is_file() and marker.read_text(encoding="utf-8").strip() == version


def _write_dep_marker(target: Path, version: str) -> None:
    (target / ".dep-version").write_text(version, encoding="utf-8")


def _download_deps(basedir: Path, upstream_godot_dir: Path, *, dry_run: bool) -> None:
    deps = basedir / "deps"
    if not dry_run:
        deps.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading dependencies into %s", deps)
    _download_accesskit(deps, upstream_godot_dir, dry_run=dry_run)
    _download_moltenvk(deps, dry_run=dry_run)
    _download_angle(deps, upstream_godot_dir, dry_run=dry_run)
    _download_winrt(deps, upstream_godot_dir, dry_run=dry_run)
    # NOTE: Mesa NIR is intentionally NOT fetched host-side. The Windows
    # container's ``install_d3d12_sdk_windows.py --mingw_prefix`` step
    # installs the Mesa NIR build the engine pins into ``bin/build_deps/mesa-*``
    # at SCons time (same single-source-of-truth principle), so a separate
    # host-side download is redundant and would force a stale version.
    _download_swappy(deps, upstream_godot_dir, dry_run=dry_run)
    _prepare_android_sign_keystore(deps, dry_run=dry_run)


def _download_accesskit(
    deps_root: Path, upstream_godot_dir: Path, *, dry_run: bool
) -> None:
    version = _engine_install_version(
        upstream_godot_dir, "install_accesskit.py", "ac_version"
    )
    url = (
        "https://github.com/godotengine/godot-accesskit-c-static/releases/"
        f"download/{version}/accesskit-c-{version}.zip"
    )
    target = deps_root / "accesskit"
    sentinel = target / "accesskit-c"  # post-extract dir
    if sentinel.is_dir() and _dep_marker_current(target, version):
        logger.info(
            "AccessKit %s already present at %s; skipping download.", version, sentinel
        )
        return
    logger.info("Fetching AccessKit C SDK %s (engine-pinned).", version)
    if dry_run:
        logger.info("[dry-run] Would download %s into %s", url, target)
        return
    if sentinel.is_dir():
        shutil.rmtree(sentinel)  # stale version -> replace
    target.mkdir(parents=True, exist_ok=True)
    archive = target / "accesskit.zip"
    _download_with_retry(url, archive)
    _extract(archive, target)
    archive.unlink(missing_ok=True)
    # The zip extracts to `accesskit-c-<version>/`; rename to a stable name.
    for child in target.iterdir():
        if child.is_dir() and child.name.startswith("accesskit-c-"):
            child.rename(sentinel)
            break
    _write_dep_marker(target, version)


def _download_moltenvk(deps_root: Path, *, dry_run: bool) -> None:
    target = deps_root / "moltenvk"
    sentinel = target / "MoltenVK" / "MoltenVK.xcframework"
    if sentinel.exists():
        logger.info("MoltenVK already present at %s; skipping download.", sentinel)
        return
    logger.info("Missing MoltenVK for macOS, downloading it.")
    if dry_run:
        logger.info("[dry-run] Would download %s into %s", _MOLTENVK_URL, target)
        return
    target.mkdir(parents=True, exist_ok=True)
    archive = target / "moltenvk.tar"
    _download_with_retry(_MOLTENVK_URL, archive)
    _extract(archive, target)
    archive.unlink(missing_ok=True)
    # Bash flattens the nested layout: move include/ and the xcframework up one
    # level so consumers see MoltenVK/MoltenVK/include -> MoltenVK/include and
    # MoltenVK/MoltenVK/static/MoltenVK.xcframework -> MoltenVK/MoltenVK.xcframework.
    nested = target / "MoltenVK" / "MoltenVK"
    if nested.is_dir():
        include_src = nested / "include"
        if include_src.is_dir():
            shutil.move(str(include_src), str(target / "MoltenVK" / "include"))
        xc_src = nested / "static" / "MoltenVK.xcframework"
        if xc_src.is_dir():
            shutil.move(str(xc_src), str(sentinel))


def _download_angle(
    deps_root: Path, upstream_godot_dir: Path, *, dry_run: bool
) -> None:
    # Version is the engine's single source of truth. The old chromium/6601.2
    # arm64-llvm bundle embedded libc++ symbols that clashed with llvm-mingw at
    # link time (the historical reason Windows arm64 was best-effort); reading
    # the engine's pin keeps us on the rebuild it expects, so arm64 links.
    version = _engine_install_version(
        upstream_godot_dir, "install_angle.py", "angle_version"
    )
    # ``angle_version`` looks like ``chromium/7219``; the ``/`` is URL-encoded.
    version_url = version.replace("/", "%2F")
    base = (
        "https://github.com/godotengine/godot-angle-static/releases/download/"
        f"{version_url}/godot-angle-static"
    )
    archives = [
        (fname, f"{base}-{variant}-release.zip")
        for fname, variant in _ANGLE_ARCH_VARIANTS
    ]
    target = deps_root / "angle"
    if _dep_marker_current(target, version):
        logger.info("ANGLE %s already extracted under %s; skipping.", version, target)
        return
    logger.info("Fetching ANGLE %s (engine-pinned) into %s", version, target)
    if dry_run:
        logger.info(
            "[dry-run] Would download %d ANGLE archives into %s", len(archives), target
        )
        return
    if target.is_dir():
        shutil.rmtree(target)  # stale version -> replace so libs cannot mix
    target.mkdir(parents=True, exist_ok=True)
    for fname, url in archives:
        archive = target / fname
        _download_with_retry(url, archive)
        _extract(archive, target)
        archive.unlink(missing_ok=True)
    _write_dep_marker(target, version)


def _download_winrt(
    deps_root: Path, upstream_godot_dir: Path, *, dry_run: bool
) -> None:
    """Fetch the WinRT (OneCore TTS) MinGW headers the Windows build needs.

    Version is the engine's single source of truth (``install_winrt.py`` —
    ``winrt_version``). The engine defaults ``winrt=yes`` and reads the headers
    from ``winrt_path``; we mount this dir into the Windows container.
    """
    version = _engine_install_version(
        upstream_godot_dir, "install_winrt.py", "winrt_version"
    )
    url = (
        "https://github.com/godotengine/winrt-mingw/releases/download/"
        f"{version}/winrt-headers.zip"
    )
    target = deps_root / "winrt"
    if _dep_marker_current(target, version):
        logger.info("WinRT %s already present at %s; skipping.", version, target)
        return
    logger.info("Fetching WinRT headers %s (engine-pinned) into %s", version, target)
    if dry_run:
        logger.info("[dry-run] Would download %s into %s", url, target)
        return
    if target.is_dir():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    archive = target / "winrt-headers.zip"
    _download_with_retry(url, archive)
    _extract(archive, target)
    archive.unlink(missing_ok=True)
    _write_dep_marker(target, version)


def _download_swappy(
    deps_root: Path, upstream_godot_dir: Path, *, dry_run: bool
) -> None:
    """Stage Swappy exactly as ``misc/scripts/install_swappy_android.py`` does.

    The engine's installer is the contract: it downloads ``swappy_filename``
    from the ``swappy_tag`` release and lays out
    ``<arch>/libswappy_static.a`` for every arch in ``swappy_archs`` under
    ``thirdparty/swappy-frame-pacing/``. ``platform/android/detect.py``
    (``detect_swappy``) probes exactly those paths, so the deps dir we hand to
    the Android container — copied verbatim into that thirdparty dir by
    :func:`scripts.in_container.common.apply_swappy` — must have the same
    arch-at-top-level shape. Filename, tag and arch list are all read from the
    engine script so a rename or an added ABI upstream cannot drift.
    """
    tag = _engine_install_version(
        upstream_godot_dir, "install_swappy_android.py", "swappy_tag"
    )
    filename = _engine_install_version(
        upstream_godot_dir, "install_swappy_android.py", "swappy_filename"
    )
    archs = _engine_install_str_list(
        upstream_godot_dir, "install_swappy_android.py", "swappy_archs"
    )
    url = (
        "https://github.com/godotengine/godot-swappy/releases/download/"
        f"{tag}/{filename}"
    )
    target = deps_root / "swappy"
    if _swappy_libs_present(target, archs) and _dep_marker_current(target, tag):
        logger.info("Swappy %s already extracted under %s; skipping.", tag, target)
        return
    logger.info("Fetching Swappy %s (engine-pinned) into %s", tag, target)
    if dry_run:
        logger.info("[dry-run] Would download %s into %s", url, target)
        return
    if target.is_dir():
        shutil.rmtree(target)  # stale/partial -> replace
    target.mkdir(parents=True, exist_ok=True)
    archive = target / filename
    _download_with_retry(url, archive)
    _extract(archive, target)
    archive.unlink(missing_ok=True)
    if not _swappy_libs_present(target, archs):
        missing = [
            arch for arch in archs if not (target / arch / _SWAPPY_LIB).is_file()
        ]
        raise _HostOrchestratorError(
            f"Swappy archive {url} did not yield {_SWAPPY_LIB} for: "
            f"{', '.join(missing)}. Expected arch dirs at the archive root, "
            f"matching install_swappy_android.py."
        )
    _write_dep_marker(target, tag)


def _swappy_libs_present(target: Path, archs: Sequence[str]) -> bool:
    return bool(archs) and all(
        (target / arch / _SWAPPY_LIB).is_file() for arch in archs
    )


def _prepare_android_sign_keystore(deps_root: Path, *, dry_run: bool) -> None:
    """Stage the Android signing keystore when GODOT_ANDROID_SIGN_KEYSTORE is set.

    Optional; downstream app-export signing is handled by the Godot exporter
    — not by the engine template build. A missing keystore is a no-op
    (logged INFO).
    """
    logger.info("Preparing android signing keystore...")
    keystore_env = os.environ.get("GODOT_ANDROID_SIGN_KEYSTORE", "")
    if not keystore_env:
        logger.info(
            "INFO: GODOT_ANDROID_SIGN_KEYSTORE is not set; skipping Android keystore staging."
        )
        return
    src = Path(keystore_env)
    if not src.is_file():
        logger.warning(
            "GODOT_ANDROID_SIGN_KEYSTORE='%s' does not point to a readable file; skipping.",
            src,
        )
        return
    dest_dir = deps_root / "keystore"
    if dry_run:
        logger.info("[dry-run] Would copy %s into %s", src, dest_dir)
        return
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest_dir / src.name)


# ---------------------------------------------------------------------------
# Source preparation
# ---------------------------------------------------------------------------


def _prepare_source(
    *,
    basedir: Path,
    upstream_godot_dir: Path,
    git_branch: str,
    version_status_patch: str,
    dry_run: bool,
) -> tuple[str, str]:
    """Generate ``godot-<version>.tar.gz`` next to *basedir* and return
    ``(godot_version, godot_version_status)``.

    Uses the LOCAL submodule's currently-checked-out state — never clones,
    resets, or switches branches. Warns when the checked-out ref differs from
    *git_branch*.
    """
    logger.info("Preparing Godot source...")
    version_py = upstream_godot_dir / "version.py"
    if not version_py.is_file():
        raise _HostOrchestratorError(
            f"Godot submodule not found at {upstream_godot_dir} (no version.py). "
            "Initialise it with: git submodule update --init upstream/godot"
        )

    current_ref = _git_current_ref(upstream_godot_dir)
    if git_branch and current_ref != git_branch:
        logger.warning(
            "upstream/godot is on %s but config git_branch is %s;",
            current_ref,
            git_branch,
        )
        logger.warning(
            "building the currently checked-out state — operator controls the "
            "submodule checkout per project rules."
        )
    logger.info(
        "Using Godot submodule at %s (ref: %s).", upstream_godot_dir, current_ref
    )

    godot_version, godot_version_status = _read_version(upstream_godot_dir)
    if version_status_patch:
        godot_version_status = f"{godot_version_status}{version_status_patch}"

    tarball_dest = basedir / f"godot-{godot_version}.tar.gz"
    if dry_run:
        logger.info(
            "[dry-run] Would run make_tarball.sh and move godot-%s.tar.gz to %s",
            godot_version,
            tarball_dest,
        )
        return godot_version, godot_version_status

    logger.info("Creating Godot tarball (version %s)...", godot_version)
    make_tarball = upstream_godot_dir / "misc" / "scripts" / "make_tarball.sh"
    if not make_tarball.is_file():
        raise _HostOrchestratorError(
            f"make_tarball.sh not found at {make_tarball}. Has the upstream submodule layout changed?"
        )
    result = subprocess.run(
        ["sh", str(make_tarball), "-v", godot_version],
        cwd=str(upstream_godot_dir),
    )
    if result.returncode != 0:
        raise _HostOrchestratorError(
            f"make_tarball.sh exited with code {result.returncode}."
        )

    # make_tarball.sh writes the tarball to the submodule's PARENT dir.
    intermediate = upstream_godot_dir.parent / f"godot-{godot_version}.tar.gz"
    if not intermediate.is_file():
        raise _HostOrchestratorError(
            f"Expected tarball at {intermediate} after make_tarball.sh; not found."
        )
    shutil.move(str(intermediate), str(tarball_dest))
    return godot_version, godot_version_status


def _git_current_ref(path: Path) -> str:
    """Return current branch name, or short commit on detached HEAD.

    Falls back to ``'HEAD'`` if git is unavailable or the dir is not a checkout.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return "HEAD"
    if result.returncode != 0:
        return "HEAD"
    ref = result.stdout.strip()
    if ref == "HEAD":
        # Detached: report short commit instead.
        try:
            short = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
            )
            if short.returncode == 0:
                return short.stdout.strip() or "HEAD"
        except OSError:
            pass
    return ref or "HEAD"


def _read_version(upstream_godot_dir: Path) -> tuple[str, str]:
    """Return ``(version, status)`` by importing ``upstream/godot/version.py``.

    Runs the read in a *subprocess* for security hardening: the upstream
    ``version.py`` is sourced from a submodule and could in principle
    execute arbitrary code on import. Sandboxing it in a child Python
    process means a malicious or corrupted ``version.py`` cannot mutate the
    orchestrator's interpreter state (``sys.modules``, env, signal handlers).
    """
    version_py = upstream_godot_dir / "version.py"
    if not version_py.is_file():
        raise _HostOrchestratorError(
            f"Failed to load version.py from {upstream_godot_dir}."
        )
    code = textwrap.dedent("""
        import sys
        sys.path.insert(0, sys.argv[1])
        import version as v
        major = getattr(v, "major", 0)
        minor = getattr(v, "minor", 0)
        patch = getattr(v, "patch", 0)
        status = getattr(v, "status", "")
        if patch:
            print(f"{major}.{minor}.{patch}")
        else:
            print(f"{major}.{minor}")
        print(status)
        """)
    try:
        result = subprocess.run(
            [sys.executable, "-c", code, str(upstream_godot_dir)],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise _HostOrchestratorError(
            f"Failed to read version.py from {upstream_godot_dir} "
            f"(exit {exc.returncode}): {exc.stderr.strip()}"
        ) from exc
    except OSError as exc:
        raise _HostOrchestratorError(
            f"Failed to invoke python to read version.py from {upstream_godot_dir}: {exc}"
        ) from exc
    lines = result.stdout.strip().splitlines()
    if not lines:
        raise _HostOrchestratorError(
            f"version.py at {upstream_godot_dir} produced no output."
        )
    version = lines[0]
    status = lines[1] if len(lines) > 1 else ""
    return version, status


# ---------------------------------------------------------------------------
# Resumability gates
# ---------------------------------------------------------------------------


def _platform_has_artifacts(out_plat: Path) -> bool:
    """Return True when *out_plat* contains any regular file at depth ≥ 2.

    Depth 1 = ``out/<plat>/`` children (category dirs); depth 2 = files
    inside those category dirs.
    """
    if not out_plat.is_dir():
        return False
    for child in out_plat.iterdir():
        if not child.is_dir():
            continue
        for grandchild in child.rglob("*"):
            if grandchild.is_file():
                return True
    return False


def _mono_glue_has_artifacts(mono_glue_dir: Path) -> bool:
    """Return True when *mono_glue_dir* contains any file at depth ≥ 1."""
    if not mono_glue_dir.is_dir():
        return False
    for path in mono_glue_dir.rglob("*"):
        if path.is_file():
            return True
    return False


# ---------------------------------------------------------------------------
# Docker run helpers
# ---------------------------------------------------------------------------


def _resolve_build_flags(build_type: str) -> tuple[bool, bool]:
    if build_type == "all":
        return True, True
    if build_type == "classical":
        return True, False
    if build_type == "mono":
        return False, True
    raise _HostOrchestratorError(
        f"Unknown build_type '{build_type}' (expected 'all', 'classical', or 'mono')."
    )


# Host-side ``scripts/`` dir is mounted read-only into every container at
# /root/build-scripts/scripts so ``python3 -m scripts.in_container.build_<plat>``
# can resolve the top-level ``scripts`` package.
#
# Why nest the mount one level deeper than PYTHONPATH? The host directory we
# bind-mount IS the ``scripts/`` package (it contains ``__init__.py`` and
# ``in_container/``). Mounting it at ``/root/build-scripts`` and setting
# ``PYTHONPATH=/root/build-scripts`` makes Python see a top-level package
# called ``in_container`` (and no ``scripts`` package at all) — so
# ``python3 -m scripts.in_container.build_<plat>`` raises
# ``ModuleNotFoundError: No module named 'scripts'``.
#
# Mounting the same host dir at ``/root/build-scripts/scripts`` instead, with
# PYTHONPATH still pointing at the parent ``/root/build-scripts``, makes
# Python see ``scripts/`` as a top-level package and the ``-m`` invocation
# resolves correctly.
_SCRIPTS_MOUNT_TARGET = "/root/build-scripts/scripts"
# Parent of the mount target — what PYTHONPATH points at so that the
# top-level package name in `python3 -m scripts.in_container.X` resolves.
_SCRIPTS_PYTHONPATH = "/root/build-scripts"


def _scripts_host_dir() -> Path:
    """Return the host-side path to the ``scripts/`` package directory.

    The orchestrator lives at ``scripts/host_orchestrator.py``; its parent
    directory is the ``scripts/`` package the in-container modules import.
    Resolving this dynamically (instead of hard-coding ``basedir``) lets the
    mount work no matter where the operator runs the build from.
    """
    return Path(__file__).resolve().parent


def _common_docker_args(
    *,
    tarball: Path,
    mono_glue_dir: Path,
    env: dict[str, str],
) -> list[str]:
    """Return the shared ``docker run`` prefix (no per-platform mounts).

    Mount + env additions for the in-container Python entry points:

      * Mounts the host's ``scripts/`` directory at
        ``/root/build-scripts/scripts`` (read-only) so the in-container
        python interpreter can import the
        ``scripts.in_container.build_<platform>`` modules.
      * Sets ``PYTHONPATH=/root/build-scripts`` (the parent of the mount
        target) so ``python3 -m scripts.in_container.build_<platform>``
        resolves ``scripts`` as a top-level package.
    """
    args = ["docker", "run", "--rm"]
    env_with_pythonpath = dict(env)
    env_with_pythonpath["PYTHONPATH"] = _SCRIPTS_PYTHONPATH
    for key, value in env_with_pythonpath.items():
        args.extend(["--env", f"{key}={value}"])
    args.extend(
        [
            "-v",
            f"{tarball}:/root/godot.tar.gz",
            "-v",
            f"{mono_glue_dir}:/root/mono-glue",
            "-v",
            f"{_scripts_host_dir()}:{_SCRIPTS_MOUNT_TARGET}:ro",
            "-w",
            "/root/",
        ]
    )
    return args


def _run_build_mono_glue(
    *,
    basedir: Path,
    linux_image: str,
    tarball: Path,
    mono_glue_dir: Path,
    run_logs_dir: Path,
    env: dict[str, str],
    dry_run: bool,
) -> None:
    del basedir  # mono glue no longer needs a host-side build/ dir mount.
    glue_env = {**env, "GODOT_BUILD_PLATFORM": "mono-glue"}
    args = _common_docker_args(
        tarball=tarball, mono_glue_dir=mono_glue_dir, env=glue_env
    )
    args.extend(["-v", f"{run_logs_dir}:/root/logs"])
    args.extend(
        [
            linux_image,
            "python3",
            "-m",
            "scripts.in_container.build_mono_glue",
        ]
    )
    _run_and_tee(
        args,
        log_path=build_logs.mono_glue_container_log(run_logs_dir),
        dry_run=dry_run,
    )


def _iter_platforms(
    *,
    basedir: Path,
    images: dict[str, str],
):
    """Yield ``(name, image, extra_mounts, extra_env)`` per platform in build order.

    Per-platform ``extra_mounts`` is a list of ``["-v", "host:container", ...]``
    fragments appended after the common docker args: the platform's ``out``
    dir plus one ``/root/<dep>`` mount per dependency declared in the
    :mod:`scripts.platforms` registry.

    Yields every platform in the registry's canonical order (Linux first).
    Apple targets are not gated — if the toolchain is missing, the
    in-container build emits an actionable error and the run fails.

    Note: no ``/root/mesa`` mount for Windows — ``install_d3d12_sdk_windows.py``
    installs Mesa NIR into ``bin/build_deps/mesa-*`` at SCons time inside the
    container; a host-side mount would force a stale version.
    """
    for name in platforms.release_order():
        plat = platforms.get(name)
        mounts = ["-v", f"{basedir / 'out' / name}:/root/out"]
        for dep in plat.deps:
            mounts += ["-v", f"{basedir / 'deps' / dep}:/root/{dep}"]
        yield (name, images[name], mounts, dict(plat.extra_env))


def _run_build_platform(
    *,
    name: str,
    image: str,
    basedir: Path,
    tarball: Path,
    mono_glue_dir: Path,
    run_logs_dir: Path,
    out_plat: Path,
    env: dict[str, str],
    extra_mounts: list[str],
    dry_run: bool,
) -> None:
    del out_plat  # mount path is already encoded in extra_mounts
    del basedir  # only used by mono-glue; per-platform mounts come from extra_mounts
    args = _common_docker_args(tarball=tarball, mono_glue_dir=mono_glue_dir, env=env)
    args.extend(["-v", f"{run_logs_dir}:/root/logs"])
    args.extend(extra_mounts)
    # The host's scripts/ dir is mounted at /root/build-scripts/scripts
    # (read-only) and PYTHONPATH points at its parent /root/build-scripts
    # (see _common_docker_args). The container invokes the per-platform
    # Python entry point directly.
    args.extend(
        [
            image,
            "python3",
            "-m",
            f"scripts.in_container.build_{name}",
        ]
    )
    _run_and_tee(
        args,
        log_path=build_logs.platform_container_log(run_logs_dir, name),
        dry_run=dry_run,
    )


def _run_and_tee(
    cmd: list[str],
    *,
    log_path: Path,
    dry_run: bool,
) -> None:
    """Run *cmd* with combined stderr→stdout, fan-out lines to host stdout and *log_path*.

    Equivalent to ``2>&1 | tee logs/<run-id>/<plat>/container.log``: high-level
    container output lands on both the operator's terminal and the platform
    ``container.log``. Verbose scons output is written per arch/target under
    the same run directory by the in-container build scripts.
    aborts the run by raising :class:`_HostOrchestratorError`.
    """
    if dry_run:
        logger.info("[dry-run] Would run: %s (log: %s)", " ".join(cmd), log_path)
        return

    logger.info("Running: %s", " ".join(cmd))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
    )
    assert process.stdout is not None
    with log_path.open("w", encoding="utf-8") as log_f:
        for line in process.stdout:
            sys.stdout.write(line)
            log_f.write(line)
    rc = process.wait()
    if rc != 0:
        raise _HostOrchestratorError(
            f"`docker run` for {' '.join(cmd[:3])} exited with code {rc}; "
            f"see {log_path} for the full log."
        )


# ---------------------------------------------------------------------------
# Final chown
# ---------------------------------------------------------------------------


def _chown_outputs(basedir: Path, *, dry_run: bool) -> None:
    """Best-effort, interruption-tolerant chown of the build outputs.

    Hands ``out/``, ``mono-glue/`` and ``godot*.tar.gz`` (produced as ``root``
    by the Docker builds) back to the invoking user so the operator can manage
    them without ``sudo``. This is purely cosmetic — every file is left
    world-readable, so packaging never depends on it — which is why the caller
    runs it *last*, after publishing.

    Resilience: every path is chowned independently inside its own ``try`` —
    a failure on one (a file owned by a different container uid, a vanished
    temp file, a refusing filesystem) is counted and skipped, never aborting
    the rest. Directory walks that error mid-stream are truncated rather than
    propagated. The function never raises, so it can sit at the tail of the
    release flow without risking the work that precedes it.
    """
    if dry_run:
        logger.info("[dry-run] Would chown out/, mono-glue/, and godot*.tar.gz.")
        return

    uid = os.environ.get("SUDO_UID")
    gid = os.environ.get("SUDO_GID")
    try:
        uid_int = int(uid) if uid is not None else os.getuid()
        gid_int = int(gid) if gid is not None else os.getgid()
    except (AttributeError, ValueError):
        logger.info("Skipping chown: cannot resolve uid/gid on this platform.")
        return

    targets: list[Path] = [basedir / "out", basedir / "mono-glue"]
    targets.extend(basedir.glob("godot*.tar.gz"))

    failures = 0
    description = f"Restoring ownership of {basedir} outputs to {uid_int}:{gid_int}..."
    with spinner(description):
        for target in targets:
            if not target.exists():
                continue
            for path in _walk_tree(target):
                try:
                    os.chown(path, uid_int, gid_int)
                except OSError:
                    # One unchangeable path must never short-circuit the rest.
                    failures += 1
    if failures:
        logger.info(
            "chown: left %d path(s) unchanged (non-fatal — e.g. owned by a "
            "different container uid, or this process is unprivileged).",
            failures,
        )


def _walk_tree(root: Path):
    """Yield *root* then every descendant, swallowing mid-walk OS errors.

    A directory that vanishes or denies listing partway through ends that
    branch instead of raising; everything already yielded still gets chowned.
    """
    yield root
    if not root.is_dir():
        return
    try:
        for child in root.rglob("*"):
            yield child
    except OSError as exc:
        logger.debug("chown walk stopped early under %s: %s", root, exc)


# ---------------------------------------------------------------------------
# Clean modes
# ---------------------------------------------------------------------------


_CLEAN_RELEASE_PATHS = (
    "mono-glue",
    "out",
    "releases",
    "tmp",
    "logs",
    "web",
    "sha512sums",
)
_CLEANUP_EXTRA_PATHS = ("git", "deps")


def _clean_release(basedir: Path, *, dry_run: bool) -> int:
    """Remove build/release artefacts but keep ``deps/``."""
    logger.info("Cleaning release artefacts under %s", basedir)
    _rm_paths(basedir, _CLEAN_RELEASE_PATHS, dry_run=dry_run)
    _rm_tarballs(basedir, dry_run=dry_run)
    return 0


def _cleanup(basedir: Path, *, dry_run: bool) -> int:
    """Remove every generated artefact including ``deps/``."""
    logger.info("Cleaning ALL artefacts (deps included) under %s", basedir)
    _rm_paths(basedir, _CLEAN_RELEASE_PATHS + _CLEANUP_EXTRA_PATHS, dry_run=dry_run)
    _rm_tarballs(basedir, dry_run=dry_run)
    return 0


def _rm_paths(basedir: Path, names: tuple[str, ...], *, dry_run: bool) -> None:
    for name in names:
        target = basedir / name
        if not target.exists():
            continue
        if dry_run:
            logger.info("[dry-run] Would rm -rf %s", target)
            continue
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()


def _rm_tarballs(basedir: Path, *, dry_run: bool) -> None:
    for tarball in basedir.glob("godot*.tar.gz"):
        if dry_run:
            logger.info("[dry-run] Would rm %s", tarball)
            continue
        tarball.unlink()


# ---------------------------------------------------------------------------
# Download + extract helpers (urllib + retry, mirrors install_d3d12 wrapper)
# ---------------------------------------------------------------------------


# Module-level so tests can monkeypatch with a faster value.
_DOWNLOAD_TIMEOUT_S = 60
_DOWNLOAD_MAX_ATTEMPTS = 4
_DOWNLOAD_BACKOFF_BASE_S = 2.0


def _download_with_retry(url: str, dest: Path) -> None:
    """Download *url* to *dest* with a 60 s socket timeout + 4-attempt backoff.

    Removes partial files between attempts so a re-extract sees no stale state.
    Raises :class:`_HostOrchestratorError` after the final attempt fails.
    """
    socket.setdefaulttimeout(_DOWNLOAD_TIMEOUT_S)
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_exc: Exception | None = None
    for attempt in range(1, _DOWNLOAD_MAX_ATTEMPTS + 1):
        logger.info(
            "Downloading %s -> %s (attempt %d/%d)",
            url,
            dest,
            attempt,
            _DOWNLOAD_MAX_ATTEMPTS,
        )
        try:
            urllib.request.urlretrieve(url, dest)
            return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc
            if dest.exists():
                try:
                    dest.unlink()
                except OSError:
                    pass
            if attempt == _DOWNLOAD_MAX_ATTEMPTS:
                break
            sleep_for = _DOWNLOAD_BACKOFF_BASE_S * (2 ** (attempt - 1))
            logger.warning(
                "Download attempt %d/%d failed (%s); retrying in %.1fs.",
                attempt,
                _DOWNLOAD_MAX_ATTEMPTS,
                exc,
                sleep_for,
            )
            time.sleep(sleep_for)
    raise _HostOrchestratorError(
        f"Failed to download {url} after {_DOWNLOAD_MAX_ATTEMPTS} attempts: {last_exc}"
    )


def _extract(archive: Path, dest: Path) -> None:
    """Extract *archive* into *dest*. Dispatches on suffix.

    ``.7z`` is delegated to the host ``7z`` binary because the stdlib has no
    LZMA2 multi-stream extractor. ``.zip``, ``.tar``, ``.tar.gz``,
    ``.tar.xz`` are handled in-process via :func:`_safe_extract` which
    rejects path-traversal entries.
    """
    dest.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    if (
        name.endswith(".zip")
        or name.endswith(".tar")
        or name.endswith(".tar.gz")
        or name.endswith(".tar.xz")
        or name.endswith(".tgz")
    ):
        _safe_extract(archive, dest)
        return
    if name.endswith(".7z"):
        # 7z extraction is delegated to the host ``7z x`` binary. Path
        # traversal in 7z archives is that tool's responsibility — every
        # archive we consume comes from a trusted GitHub Release, and any
        # general-purpose 7z hardening belongs in the 7z package itself.
        _extract_7z(archive, dest)
        return
    raise _HostOrchestratorError(f"Unsupported archive format: {archive}")


def _safe_extract(archive: Path, dest: Path) -> None:
    """Extract *archive* into *dest*, rejecting path-traversal entries.

    Every entry's resolved path must stay inside ``dest.resolve()``. Any
    ``../etc/passwd``-style entry, absolute path, or symlink target that
    escapes the destination raises :class:`_HostOrchestratorError` before
    any byte is written.

    Behaviour by archive type:

      * ``.zip`` — iterate :meth:`zipfile.ZipFile.infolist` and validate each
        entry's resolved destination is a subpath of ``dest``. There is no
        stdlib ``filter=`` for zip, so the loop is explicit.
      * ``.tar`` / ``.tar.gz`` / ``.tar.xz`` / ``.tgz`` — on Python ≥ 3.12 we
        pass ``filter="data"`` (the official safe filter that strips absolute
        paths, leading ``../`` segments, device files, and dangerous links).
        On older runtimes we apply the same manual subpath check used for
        zips and additionally validate symlink/hardlink targets.
    """
    dest.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve()
    name = archive.name.lower()

    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for member in zf.infolist():
                _reject_unsafe_member(archive, member.filename, dest_resolved)
            zf.extractall(dest)
        return

    if (
        name.endswith(".tar")
        or name.endswith(".tar.gz")
        or name.endswith(".tar.xz")
        or name.endswith(".tgz")
    ):
        with tarfile.open(archive) as tf:
            if sys.version_info >= (3, 12):
                # ``filter="data"`` is the official tarfile safe filter
                # (PEP 706): rejects absolute paths, leading ``..``, device
                # files, and links pointing outside the destination.
                tf.extractall(dest, filter="data")
                return
            for member in tf.getmembers():
                _reject_unsafe_member(archive, member.name, dest_resolved)
                if member.issym() or member.islnk():
                    _reject_unsafe_link_target(
                        archive, member.name, member.linkname, dest_resolved
                    )
            tf.extractall(dest)
        return

    raise _HostOrchestratorError(f"Unsupported archive format: {archive}")


def _reject_unsafe_member(archive: Path, member_name: str, dest_resolved: Path) -> None:
    """Raise if *member_name* would resolve outside *dest_resolved*."""
    # ``Path(...).resolve()`` collapses ``..`` segments and absolute paths.
    # ``is_relative_to`` (Python 3.9+) is the canonical subpath check.
    candidate = (dest_resolved / member_name).resolve()
    if candidate != dest_resolved and not candidate.is_relative_to(dest_resolved):
        raise _HostOrchestratorError(
            f"Refusing to extract {archive}: entry '{member_name}' resolves "
            f"to {candidate}, which is outside {dest_resolved}."
        )


def _reject_unsafe_link_target(
    archive: Path,
    member_name: str,
    link_name: str,
    dest_resolved: Path,
) -> None:
    """Raise if a symlink/hardlink target points outside *dest_resolved*."""
    # Absolute link targets are unambiguously unsafe.
    link_path = Path(link_name)
    if link_path.is_absolute():
        raise _HostOrchestratorError(
            f"Refusing to extract {archive}: entry '{member_name}' has an "
            f"absolute link target '{link_name}'."
        )
    # Relative targets are resolved against the link's directory.
    member_dir = (dest_resolved / member_name).parent
    target = (member_dir / link_name).resolve()
    if not target.is_relative_to(dest_resolved):
        raise _HostOrchestratorError(
            f"Refusing to extract {archive}: entry '{member_name}' links to "
            f"{target}, which is outside {dest_resolved}."
        )


def _extract_7z(archive: Path, dest: Path) -> None:
    """Shell out to ``7z x`` (no good stdlib equivalent for LZMA2 multi-stream).

    The host MUST have the ``7zip`` package installed; otherwise we raise a
    clear error. Bash had the same dependency.
    """
    seven_zip = shutil.which("7z") or shutil.which("7zz")
    if seven_zip is None:
        raise _HostOrchestratorError(
            f"Cannot extract {archive}: `7z` binary not found on PATH. "
            "Install the host 7zip package."
        )
    result = subprocess.run(
        [seven_zip, "x", f"-o{dest}", "-y", str(archive)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise _HostOrchestratorError(
            f"7z extraction of {archive} failed (rc={result.returncode}): {result.stderr.strip()}"
        )
