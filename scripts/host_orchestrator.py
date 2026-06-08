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
     ``MONO``. ``tee``-style log fan-out into ``out/logs/<plat>``.
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
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — dependency download URLs (one per ``download_*`` helper below)
# ---------------------------------------------------------------------------

_MOLTENVK_URL = (
    "https://github.com/godotengine/moltenvk-osxcross/releases/download/"
    "vulkan-sdk-1.3.283.0-2/MoltenVK-all.tar"
)

_ACCESSKIT_URL = (
    "https://github.com/godotengine/godot-accesskit-c-static/releases/download/"
    "0.21.2/accesskit-c-0.21.2.zip"
)

_ANGLE_BASE_URL = (
    "https://github.com/godotengine/godot-angle-static/releases/download/"
    "chromium%2F6601.2/godot-angle-static"
)
_ANGLE_ARCHIVES: tuple[tuple[str, str], ...] = (
    ("windows_arm64.zip", f"{_ANGLE_BASE_URL}-arm64-llvm-release.zip"),
    ("windows_x86_64.zip", f"{_ANGLE_BASE_URL}-x86_64-gcc-release.zip"),
    ("windows_x86_32.zip", f"{_ANGLE_BASE_URL}-x86_32-gcc-release.zip"),
    ("macos_arm64.zip", f"{_ANGLE_BASE_URL}-arm64-macos-release.zip"),
    ("macos_x86_64.zip", f"{_ANGLE_BASE_URL}-x86_64-macos-release.zip"),
)

_SWAPPY_URL = (
    "https://github.com/godotengine/godot-swappy/releases/download/"
    "from-source-2025-01-31/godot-swappy.7z"
)


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
    skip_download_containers: bool = False,
    build_name: str = "",
    version_status_patch: str = "",
    dry_run: bool = False,
    mode: _Mode = "build",
) -> int:
    """Run the host-side build dispatcher in-process.

    *godot_repo* is accepted for caller convenience but is not used by the
    build flow itself — the engine source is always the local
    ``upstream_godot_dir`` submodule.

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

    try:
        if not skip_download_containers:
            _pull_images(images, dry_run=dry_run)
        else:
            logger.info("Skipping `docker pull` (skip_download_containers=True).")

        _download_deps(basedir, dry_run=dry_run)
        godot_version, godot_version_status = _prepare_source(
            basedir=basedir,
            upstream_godot_dir=upstream_godot_dir,
            git_branch=git_branch,
            version_status_patch=version_status_patch,
            dry_run=dry_run,
        )

        out_dir = basedir / "out"
        logs_dir = out_dir / "logs"
        mono_glue_dir = basedir / "mono-glue"
        if not dry_run:
            out_dir.mkdir(parents=True, exist_ok=True)
            logs_dir.mkdir(parents=True, exist_ok=True)
            mono_glue_dir.mkdir(parents=True, exist_ok=True)

        build_classical, build_mono = _resolve_build_flags(build_type)
        common_env = {
            "BUILD_NAME": build_name,
            "GODOT_VERSION_STATUS": godot_version_status,
            "NUM_CORES": str(num_cores),
            "CLASSICAL": "1" if build_classical else "0",
            "MONO": "1" if build_mono else "0",
        }
        tarball_path = basedir / f"godot-{godot_version}.tar.gz"

        # --- Mono glue ---
        if _mono_glue_has_artifacts(mono_glue_dir):
            logger.info(
                "Skipping mono-glue: %s already populated. Delete it to force a rebuild.",
                mono_glue_dir,
            )
        else:
            _run_build_mono_glue(
                basedir=basedir,
                linux_image=images["linux"],
                tarball=tarball_path,
                mono_glue_dir=mono_glue_dir,
                logs_dir=logs_dir,
                env=common_env,
                dry_run=dry_run,
            )

        # --- Per-platform ---
        for plat, image, extra_mounts, extra_env in _iter_platforms(
            basedir=basedir,
            images=images,
        ):
            out_plat = out_dir / plat
            if not dry_run:
                out_plat.mkdir(parents=True, exist_ok=True)
            if _platform_has_artifacts(out_plat):
                logger.info(
                    "Skipping %s: artifacts already present in %s. "
                    "Delete them to force a rebuild.",
                    plat,
                    out_plat,
                )
                continue
            _run_build_platform(
                name=plat,
                image=image,
                basedir=basedir,
                tarball=tarball_path,
                mono_glue_dir=mono_glue_dir,
                out_plat=out_plat,
                logs_dir=logs_dir,
                env={**common_env, **extra_env},
                extra_mounts=extra_mounts,
                dry_run=dry_run,
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
    """Resolve the 6 platform images per config (no BASE_DISTRO suffix).

    Tags resolve as ``${registry}/${username}/godot-<plat>:${container_version}``
    and MUST match what ``containers --push`` produces and what config.toml
    declares.
    """
    return {
        "windows": f"{registry}/{username}/godot-windows:{container_version}",
        "linux": f"{registry}/{username}/godot-linux:{container_version}",
        "web": f"{registry}/{username}/godot-web:{container_version}",
        "macos": f"{registry}/{username}/godot-osx:{container_version}",
        "android": f"{registry}/{username}/godot-android:{container_version}",
        "ios": f"{registry}/{username}/godot-ios:{container_version}",
    }


def _pull_images(images: dict[str, str], *, dry_run: bool) -> None:
    logger.info("Fetching images from GitHub Container Registry...")
    for plat, image in images.items():
        if dry_run:
            logger.info("[dry-run] Would `docker pull %s` (%s)", image, plat)
            continue
        logger.info("Pulling image: %s", image)
        result = subprocess.run(["docker", "pull", image])
        if result.returncode != 0:
            raise _HostOrchestratorError(
                f"`docker pull {image}` exited with code {result.returncode}."
            )


# ---------------------------------------------------------------------------
# Dependency download
# ---------------------------------------------------------------------------


def _download_deps(basedir: Path, *, dry_run: bool) -> None:
    deps = basedir / "deps"
    if not dry_run:
        deps.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading dependencies into %s", deps)
    _download_accesskit(deps, dry_run=dry_run)
    _download_moltenvk(deps, dry_run=dry_run)
    _download_angle(deps, dry_run=dry_run)
    # NOTE: Mesa NIR is intentionally NOT fetched host-side. The Windows
    # container's ``install_d3d12_sdk_windows.py --mingw_prefix`` step
    # installs the current Mesa NIR build (25.3.1-2) into
    # ``bin/build_deps/mesa-*`` at SCons time, so a separate host-side
    # download is redundant and forces a stale version on every build.
    _download_swappy(deps, dry_run=dry_run)
    _prepare_android_sign_keystore(deps, dry_run=dry_run)


def _download_accesskit(deps_root: Path, *, dry_run: bool) -> None:
    target = deps_root / "accesskit"
    sentinel = target / "accesskit-c"  # post-extract dir
    if sentinel.is_dir():
        logger.info("AccessKit already present at %s; skipping download.", sentinel)
        return
    logger.info("Missing AccessKit C SDK, downloading it.")
    if dry_run:
        logger.info("[dry-run] Would download %s into %s", _ACCESSKIT_URL, target)
        return
    target.mkdir(parents=True, exist_ok=True)
    archive = target / "accesskit.zip"
    _download_with_retry(_ACCESSKIT_URL, archive)
    _extract(archive, target)
    archive.unlink(missing_ok=True)
    # The zip extracts to `accesskit-c-<version>/`; rename to a stable name.
    for child in target.iterdir():
        if child.is_dir() and child.name.startswith("accesskit-c-"):
            child.rename(sentinel)
            break


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


def _download_angle(deps_root: Path, *, dry_run: bool) -> None:
    target = deps_root / "angle"
    # ANGLE extracts to multiple per-arch dirs; pick one we always expect.
    sentinel_files = (
        target / "lib" / "libANGLE.windows.arm64.a",
        target / "lib" / "libANGLE.windows.x86_64.a",
    )
    if any(p.exists() for p in sentinel_files):
        logger.info("ANGLE already extracted under %s; skipping.", target)
        return
    logger.info("Downloading ANGLE libraries...")
    if dry_run:
        logger.info(
            "[dry-run] Would download %d ANGLE archives into %s",
            len(_ANGLE_ARCHIVES),
            target,
        )
        return
    target.mkdir(parents=True, exist_ok=True)
    for fname, url in _ANGLE_ARCHIVES:
        archive = target / fname
        _download_with_retry(url, archive)
        _extract(archive, target)
        archive.unlink(missing_ok=True)


def _download_swappy(deps_root: Path, *, dry_run: bool) -> None:
    target = deps_root / "swappy"
    # godot-swappy.7z extracts to a `godot-swappy/` subdir.
    sentinel = target / "godot-swappy"
    if sentinel.is_dir() and any(sentinel.iterdir()):
        logger.info("Swappy already extracted under %s; skipping.", target)
        return
    # Gap fix: detect partial state — 7z present but never extracted. Re-extract.
    target.mkdir(parents=True, exist_ok=True)
    archive = target / "godot-swappy.7z"
    if archive.is_file() and not sentinel.is_dir():
        logger.warning(
            "Swappy archive present at %s but no extracted dir; re-extracting.",
            archive,
        )
        _extract(archive, target)
        archive.unlink(missing_ok=True)
        return
    logger.info("Missing Swappy libraries, downloading them.")
    if dry_run:
        logger.info("[dry-run] Would download %s into %s", _SWAPPY_URL, target)
        return
    _download_with_retry(_SWAPPY_URL, archive)
    _extract(archive, target)
    archive.unlink(missing_ok=True)


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
    code = textwrap.dedent(
        """
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
        """
    )
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
    logs_dir: Path,
    env: dict[str, str],
    dry_run: bool,
) -> None:
    del basedir  # mono glue no longer needs a host-side build/ dir mount.
    args = _common_docker_args(tarball=tarball, mono_glue_dir=mono_glue_dir, env=env)
    args.extend(
        [
            linux_image,
            "python3",
            "-m",
            "scripts.in_container.build_mono_glue",
        ]
    )
    _run_and_tee(args, log_path=logs_dir / "mono-glue", dry_run=dry_run)


def _iter_platforms(
    *,
    basedir: Path,
    images: dict[str, str],
):
    """Yield ``(name, image, extra_mounts, extra_env)`` per platform in build order.

    Per-platform ``extra_mounts`` is a list of ``["-v", "host:container", ...]``
    fragments appended after the common docker args.

    Always yields all six platforms (Linux, Android, Windows, macOS, iOS, Web).
    Apple targets are not gated — if the toolchain is missing, the
    in-container build emits an actionable error and the run fails.
    """
    # Linux
    yield (
        "linux",
        images["linux"],
        [
            "-v",
            f"{basedir / 'out' / 'linux'}:/root/out",
            "-v",
            f"{basedir / 'deps' / 'accesskit'}:/root/accesskit",
        ],
        {},
    )
    # Android
    yield (
        "android",
        images["android"],
        [
            "-v",
            f"{basedir / 'out' / 'android'}:/root/out",
            "-v",
            f"{basedir / 'deps' / 'swappy'}:/root/swappy",
            "-v",
            f"{basedir / 'deps' / 'keystore'}:/root/keystore",
        ],
        {},
    )
    # Windows — STEAM env is intentionally left at 0.
    # No /root/mesa mount: ``install_d3d12_sdk_windows.py --mingw_prefix``
    # installs Mesa NIR into ``bin/build_deps/mesa-*`` at SCons time inside
    # the container; a host-side mount would force a stale version.
    yield (
        "windows",
        images["windows"],
        [
            "-v",
            f"{basedir / 'out' / 'windows'}:/root/out",
            "-v",
            f"{basedir / 'deps' / 'angle'}:/root/angle",
            "-v",
            f"{basedir / 'deps' / 'accesskit'}:/root/accesskit",
        ],
        {"STEAM": "0"},
    )
    # Apple targets — always yielded. The in-container build raises a clear
    # actionable error if the toolchain (Xcode SDK, Swift) is not set up.
    yield (
        "macos",
        images["macos"],
        [
            "-v",
            f"{basedir / 'out' / 'macos'}:/root/out",
            "-v",
            f"{basedir / 'deps' / 'moltenvk'}:/root/moltenvk",
            "-v",
            f"{basedir / 'deps' / 'angle'}:/root/angle",
            "-v",
            f"{basedir / 'deps' / 'accesskit'}:/root/accesskit",
        ],
        {},
    )
    yield (
        "ios",
        images["ios"],
        [
            "-v",
            f"{basedir / 'out' / 'ios'}:/root/out",
        ],
        {},
    )
    # Web — listed last.
    yield (
        "web",
        images["web"],
        [
            "-v",
            f"{basedir / 'out' / 'web'}:/root/out",
        ],
        {},
    )


def _run_build_platform(
    *,
    name: str,
    image: str,
    basedir: Path,
    tarball: Path,
    mono_glue_dir: Path,
    out_plat: Path,
    logs_dir: Path,
    env: dict[str, str],
    extra_mounts: list[str],
    dry_run: bool,
) -> None:
    del out_plat  # mount path is already encoded in extra_mounts
    del basedir  # only used by mono-glue; per-platform mounts come from extra_mounts
    args = _common_docker_args(tarball=tarball, mono_glue_dir=mono_glue_dir, env=env)
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
    log_path = logs_dir / name
    _run_and_tee(args, log_path=log_path, dry_run=dry_run)


def _run_and_tee(
    cmd: list[str],
    *,
    log_path: Path,
    dry_run: bool,
) -> None:
    """Run *cmd* with combined stderr→stdout, fan-out lines to host stdout and *log_path*.

    Equivalent to ``2>&1 | tee out/logs/<plat>``: every line lands on both the
    operator's terminal and the per-platform log file. A non-zero docker exit
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


_CLEAN_RELEASE_PATHS = ("mono-glue", "out", "releases", "tmp", "web", "sha512sums")
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
        # traversal in 7z archives is the host tool's responsibility — we
        # only consume Swappy from a trusted GitHub Release, and any
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
