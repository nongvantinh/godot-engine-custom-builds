"""Shared helpers for the per-platform in-container build modules.

Used by ``scripts.in_container.build_*`` modules running inside the
per-platform docker images. Stdlib-only.

This module centralises the patterns that recur across the per-platform
build modules:

  * :func:`run_scons` — wraps every ``scons -j${NUM_CORES} ...`` invocation,
    including the verbose/warnings/progress/redirect_build_objects defaults
    every script sets identically and the ``check=False`` (set +e) escape hatch
    Windows arm64 needs.
  * :func:`setup_godot_source` — the ``rm -rf godot && mkdir godot && cd godot
    && tar xf /root/godot.tar.gz --strip-components=1`` ritual at the top of
    every script.
  * :func:`copy_mono_glue` — the ``cp -r /root/mono-glue/.../Generated
    modules/mono/glue/.../`` block before every Mono pass.
  * :func:`apply_swappy` — Android's swappy thirdparty copy.
  * :func:`copy_and_clean_bin` — Windows-specific ``bin/`` → ``out/`` move that
    must preserve ``bin/build_deps/`` across rm cycles so install_d3d12 deps
    survive into the next scons invocation.
  * :func:`install_d3d12_sdk` — Windows ``install_d3d12_sdk_windows.py`` wrapper
    with a 10-min timeout + 4-attempt retry to cope with transient hangs.
  * :func:`swiftly_install` — iOS Swift toolchain installer; non-interactive
    flags. PGP verification IS performed: Swift 6.2.1 is pre-installed at
    image-build time in ``containers/Dockerfile.osx`` with full GPG
    verification, and ``gpgconf --kill all`` clears the stale agent sockets
    that otherwise caused runtime gpg-import failures. At runtime the
    toolchain is already present, so this function short-circuits on the
    existing ``swift-frontend``; if a non-default ``SWIFT_VERSION`` is
    requested, swiftly's runtime install runs WITH verification against the
    host ``/root/.gnupg/`` keyring (no ``--no-verify`` escape hatch).
  * :func:`extract_tarball` — ``tar xf <src> --strip-components=1 -C <dest>``
    equivalent (used for the godot tarball + any future archive deps).
  * :func:`build_mono_assemblies` — ``./modules/mono/build_scripts/
    build_assemblies.py --godot-output-dir=./bin --godot-platform=<plat>``
    invocation shared by Linux/Windows/macOS Mono editor passes.
  * :func:`gradle_wrapper` — Android ``./gradlew <task>`` invocation from
    ``platform/android/java/`` (kept here so tests can mock it once).

Logging convention mirrors :mod:`scripts.host_orchestrator`: INFO for happy
paths, WARNING for recoverable issues (Windows arm64 link clash, Android
unsigned build, missing Web mono glue), ERROR via raised exceptions.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tarfile
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InContainerBuildError(RuntimeError):
    """Raised on a hard error inside an in-container build script."""


# ---------------------------------------------------------------------------
# SCons wrapper — every script's ``$SCONS`` variable
# ---------------------------------------------------------------------------


# Common scons flags shared by every per-platform build. Kept module-level
# so tests can assert on the exact prefix without re-deriving it.
SCONS_COMMON_FLAGS: tuple[str, ...] = (
    "verbose=yes",
    "warnings=no",
    "progress=no",
    "redirect_build_objects=no",
)


def run_scons(
    *args: str,
    num_cores: int,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    check: bool = True,
) -> int:
    """Run ``scons -j<num_cores> <SCONS_COMMON_FLAGS> <args>``.

    Parameters
    ----------
    args
        Extra scons arguments (``platform=...``, ``arch=...``, ``target=...``,
        any ``OPTIONS``/``OPTIONS_MONO`` flag tokens, etc.).
    num_cores
        Threaded into ``-j``. Comes from ``NUM_CORES`` env var.
    env
        Full environment for the subprocess. When ``None`` we use
        :data:`os.environ`. Callers that need to mutate PATH for buildroot
        SDKs (Linux) build a dict explicitly.
    cwd
        Working directory. Defaults to current directory; build scripts
        ``cd godot/`` before any scons call, so we follow suit.
    check
        When ``True`` (default), a non-zero exit raises
        :class:`InContainerBuildError`. When ``False`` we return the exit
        code — used for the Windows arm64 best-effort wrapper.

    Returns
    -------
    int
        Scons exit code (always 0 when ``check=True`` and we return).
    """
    cmd: list[str] = ["scons", f"-j{num_cores}", *SCONS_COMMON_FLAGS, *args]
    logger.info("Running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        env=dict(env) if env is not None else None,
        cwd=str(cwd) if cwd is not None else None,
    )
    if check and result.returncode != 0:
        raise InContainerBuildError(
            f"scons exited with code {result.returncode}: {' '.join(cmd)}"
        )
    return result.returncode


# ---------------------------------------------------------------------------
# Source preparation — ``tar xf /root/godot.tar.gz --strip-components=1``
# ---------------------------------------------------------------------------


def extract_tarball(src: Path, dest: Path, *, strip_components: int = 1) -> None:
    """Extract *src* into *dest* with the equivalent of ``--strip-components=N``.

    The stdlib :mod:`tarfile` has no first-class strip-components flag, so we
    rewrite each member's path before extracting.
    """
    if not src.is_file():
        raise InContainerBuildError(f"Tarball not found: {src}")
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(src) as tf:
        for member in tf.getmembers():
            parts = Path(member.name).parts
            if len(parts) <= strip_components:
                # Member is at-or-above the strip depth; drop it.
                continue
            member.name = str(Path(*parts[strip_components:]))
            tf.extract(member, dest)


def setup_godot_source(
    tarball: Path = Path("/root/godot.tar.gz"),
    dest_root: Path = Path("/root"),
) -> Path:
    """Implement the ``rm -rf godot && mkdir godot && tar xf ...`` ritual.

    Returns the path to the extracted source root (``<dest_root>/godot``).
    Every per-platform script runs this exact sequence at the top.
    """
    godot_dir = dest_root / "godot"
    if godot_dir.exists():
        logger.info("Removing stale source tree at %s", godot_dir)
        shutil.rmtree(godot_dir)
    godot_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Extracting %s into %s (strip 1)", tarball, godot_dir)
    extract_tarball(tarball, godot_dir, strip_components=1)
    return godot_dir


# ---------------------------------------------------------------------------
# Mono glue copy
# ---------------------------------------------------------------------------


def copy_mono_glue(
    source_root: Path,
    godot_dir: Path,
    *,
    include_editor: bool = True,
) -> None:
    """Copy generated Mono glue into ``modules/mono/glue/`` under *godot_dir*.

    The Editor copy is only relevant when the target builds the editor
    (Linux/Windows/macOS Mono editor passes). iOS/Android/Web only need the
    runtime glue, so ``include_editor=False`` matches those callers.
    """
    runtime_src = source_root / "GodotSharp" / "GodotSharp" / "Generated"
    runtime_dest = (
        godot_dir
        / "modules"
        / "mono"
        / "glue"
        / "GodotSharp"
        / "GodotSharp"
        / "Generated"
    )
    if not runtime_src.is_dir():
        raise InContainerBuildError(
            f"Mono glue not found at {runtime_src}; "
            "did the mono-glue build run before this platform?"
        )
    _copytree_overwrite(runtime_src, runtime_dest)

    if include_editor:
        editor_src = source_root / "GodotSharp" / "GodotSharpEditor" / "Generated"
        editor_dest = (
            godot_dir
            / "modules"
            / "mono"
            / "glue"
            / "GodotSharp"
            / "GodotSharpEditor"
            / "Generated"
        )
        if not editor_src.is_dir():
            raise InContainerBuildError(
                f"Mono editor glue not found at {editor_src}; "
                "did the mono-glue build run before this platform?"
            )
        _copytree_overwrite(editor_src, editor_dest)


def apply_swappy(
    swappy_dir: Path,
    godot_dir: Path,
) -> None:
    """Copy ``/root/swappy/*`` into ``thirdparty/swappy-frame-pacing/``.

    Required by the Android build before any scons invocation.
    """
    if not swappy_dir.is_dir():
        raise InContainerBuildError(f"Swappy dir not found: {swappy_dir}")
    target = godot_dir / "thirdparty" / "swappy-frame-pacing"
    target.mkdir(parents=True, exist_ok=True)
    for child in swappy_dir.iterdir():
        dest = target / child.name
        if child.is_dir():
            _copytree_overwrite(child, dest)
        else:
            shutil.copy2(child, dest)


def _copytree_overwrite(src: Path, dest: Path) -> None:
    """Recursive copy with overwrite semantics (Python 3.8+ ``dirs_exist_ok``)."""
    shutil.copytree(src, dest, dirs_exist_ok=True)


# ---------------------------------------------------------------------------
# Windows-specific: copy_and_clean_bin
# ---------------------------------------------------------------------------


def copy_and_clean_bin(
    bin_dir: Path,
    dest: Path,
    *,
    preserve_names: Iterable[str] = ("build_deps",),
) -> None:
    """Move every entry under *bin_dir* into *dest*, preserving *preserve_names*.

    The ``build_deps/`` skip is load-bearing: ``install_d3d12_sdk_windows.py``
    writes Mesa NIR + DirectX Agility SDK + WinPixEventRuntime into
    ``bin/build_deps/`` once per container lifetime, and subsequent scons
    invocations expect them to still be there. Stripping that dir between
    arch cycles would force a re-install (and we lose the 10-min retry
    budget).
    """
    if not bin_dir.is_dir():
        # Over a missing dir: no-op.
        logger.info(
            "copy_and_clean_bin: %s does not exist; nothing to move into %s.",
            bin_dir,
            dest,
        )
        return
    preserve = set(preserve_names)
    dest.mkdir(parents=True, exist_ok=True)
    for entry in list(bin_dir.iterdir()):
        if entry.name in preserve:
            continue
        target = dest / entry.name
        # Directories are copied recursively, files keep their stat (mtime +
        # mode). shutil.copy2/copytree preserves both.
        if entry.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(entry, target)
            shutil.rmtree(entry)
        else:
            shutil.copy2(entry, target)
            entry.unlink()


def clean_bin_preserving(
    bin_dir: Path,
    *,
    preserve_names: Iterable[str] = ("build_deps",),
) -> None:
    """Wipe every entry under *bin_dir* except *preserve_names*.

    Used between best-effort arm64 builds and the next pass.
    """
    if not bin_dir.is_dir():
        return
    preserve = set(preserve_names)
    for entry in list(bin_dir.iterdir()):
        if entry.name in preserve:
            continue
        try:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except OSError as exc:
            # Non-fatal removal failure.
            logger.debug("clean_bin_preserving: ignoring %s: %s", entry, exc)


# ---------------------------------------------------------------------------
# Windows-specific: install_d3d12_sdk_windows.py wrapper
# ---------------------------------------------------------------------------


_D3D12_DEFAULT_ATTEMPTS = 4
_D3D12_DEFAULT_TIMEOUT_S = 600
_D3D12_BACKOFF_S = 20


def install_d3d12_sdk(
    godot_dir: Path,
    *,
    mingw_prefix: str | None = "/root/llvm-mingw",
    attempts: int = _D3D12_DEFAULT_ATTEMPTS,
    timeout_per_attempt: int = _D3D12_DEFAULT_TIMEOUT_S,
    sleep_fn=time.sleep,
) -> None:
    """Run ``misc/scripts/install_d3d12_sdk_windows.py`` with timeout + retry.

    Wraps the upstream installer with the resilience the Windows build needs:

      * ``timeout 600`` per attempt (urllib has no default socket timeout, so a
        stuck connection would hang forever).
      * 4 attempts with 20 s sleep between (transient mirror flakes).
      * ``--mingw_prefix`` so the installer can locate
        ``x86_64-w64-mingw32-dlltool`` and convert WinPixEventRuntime to
        MinGW-linkable libs.

    Aborts with a clear error if the installer script is missing from the
    extracted Godot tree — same as bash.
    """
    installer = godot_dir / "misc" / "scripts" / "install_d3d12_sdk_windows.py"
    if not installer.is_file():
        raise InContainerBuildError(
            "misc/scripts/install_d3d12_sdk_windows.py missing from Godot source tree; "
            "cannot install D3D12 deps."
        )

    py = shutil.which("python") or shutil.which("python3")
    if py is None:
        raise InContainerBuildError(
            "Cannot install D3D12 SDK: no `python` or `python3` on PATH."
        )

    cmd: list[str] = [py, str(installer)]
    if mingw_prefix:
        cmd.append(f"--mingw_prefix={mingw_prefix}")

    last_rc = 1
    for attempt in range(1, attempts + 1):
        logger.info(
            "Installing Direct3D 12 SDK dependencies (attempt %d/%d, %ds timeout)...",
            attempt,
            attempts,
            timeout_per_attempt,
        )
        try:
            result = subprocess.run(
                cmd,
                cwd=str(godot_dir),
                timeout=timeout_per_attempt,
            )
            last_rc = result.returncode
            if last_rc == 0:
                logger.info("install_d3d12_sdk_windows.py succeeded.")
                return
        except subprocess.TimeoutExpired:
            last_rc = 124  # mirrors GNU `timeout` exit code
            logger.warning(
                "install_d3d12_sdk_windows.py attempt %d/%d timed out after %ds.",
                attempt,
                attempts,
                timeout_per_attempt,
            )
        if attempt < attempts:
            logger.warning(
                "install_d3d12_sdk_windows.py attempt %d/%d failed (exit %d); "
                "retrying after %ds...",
                attempt,
                attempts,
                last_rc,
                _D3D12_BACKOFF_S,
            )
            sleep_fn(_D3D12_BACKOFF_S)
    raise InContainerBuildError(
        f"install_d3d12_sdk_windows.py failed after {attempts} attempts "
        f"(last exit {last_rc})."
    )


# ---------------------------------------------------------------------------
# iOS-specific: swiftly install
# ---------------------------------------------------------------------------


def swiftly_install(
    version: str,
    *,
    swiftly_home: Path = Path("/root/.local/share/swiftly"),
    swiftly_bin: Path | None = None,
) -> Path:
    """Ensure Swift *version* is installed via swiftly; return its toolchain root.

    Flags:

      * ``--assume-yes`` — non-interactive (no prompts).
      * ``--post-install-file=/dev/null`` — discard any post-install shell
        script swiftly would otherwise want us to source; we address the
        toolchain by absolute path so env mutation is unnecessary.

    PGP verification: Swift 6.2.1 is pre-installed at image-build time in
    ``containers/Dockerfile.osx`` with full GPG verification against the
    official Swift project signing keys, AND ``gpgconf --kill all`` clears
    stale gpg-agent sockets that otherwise caused ``gpg --import`` to time
    out at runtime. At runtime this function sees the pre-installed
    toolchain and short-circuits. If a non-default ``SWIFT_VERSION`` is ever
    requested, swiftly's runtime install runs WITH verification against the
    host keyring (no ``--no-verify``).

    Returns the toolchain root (``<swiftly_home>/toolchains/<version>``) — the
    iOS/macOS scripts pass ``<root>/usr/bin/swift-frontend`` to scons as the
    ``SWIFT_FRONTEND=...`` option.

    Raises :class:`InContainerBuildError` if swiftly is missing or the install
    completes but ``swift-frontend`` is still absent.
    """
    if swiftly_bin is None:
        swiftly_bin = swiftly_home / "bin" / "swiftly"
    toolchain = swiftly_home / "toolchains" / version
    swift_frontend = toolchain / "usr" / "bin" / "swift-frontend"

    if swift_frontend.is_file() and os.access(swift_frontend, os.X_OK):
        logger.info(
            "Swift %s already installed at %s; skipping install.", version, toolchain
        )
        return toolchain

    logger.info("Swift %s not installed; installing via swiftly...", version)
    if not (swiftly_bin.is_file() and os.access(swiftly_bin, os.X_OK)):
        raise InContainerBuildError(
            f"swiftly not found at {swiftly_bin}; cannot install Swift {version}."
        )

    # No ``--no-verify``: Dockerfile.osx pre-installs Swift 6.2.1 at
    # image-build time with full PGP verification, and cleans gpg-agent
    # stale sockets so that runtime ``swiftly install`` of any other version
    # can also verify against /root/.gnupg/ without timing out.
    cmd: list[str] = [
        str(swiftly_bin),
        "install",
        "--assume-yes",
        "--post-install-file=/dev/null",
        version,
    ]
    logger.info("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise InContainerBuildError(
            f"'swiftly install {version}' failed with exit {result.returncode}."
        )

    if not (swift_frontend.is_file() and os.access(swift_frontend, os.X_OK)):
        raise InContainerBuildError(
            f"Swift {version} install reported success but swift-frontend "
            f"not found at {swift_frontend}."
        )
    logger.info("Using Swift %s at %s", version, toolchain)
    return toolchain


# ---------------------------------------------------------------------------
# Shared subprocess helpers
# ---------------------------------------------------------------------------


def build_mono_assemblies(
    godot_dir: Path,
    *,
    godot_platform: str,
    output_dir: str = "./bin",
) -> None:
    """Invoke ``./modules/mono/build_scripts/build_assemblies.py``.

    Linux/Windows/macOS Mono editor passes all run this after the editor scons
    invocation. iOS/Android Mono builds skip it (templates don't need the
    managed assemblies).
    """
    script = godot_dir / "modules" / "mono" / "build_scripts" / "build_assemblies.py"
    if not script.is_file():
        raise InContainerBuildError(
            f"build_assemblies.py missing at {script}; "
            "did the Godot source extract correctly?"
        )
    py = shutil.which("python") or shutil.which("python3") or "python3"
    cmd: list[str] = [
        py,
        str(script),
        f"--godot-output-dir={output_dir}",
        f"--godot-platform={godot_platform}",
    ]
    logger.info("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, cwd=str(godot_dir))
    if result.returncode != 0:
        raise InContainerBuildError(
            f"build_assemblies.py failed with exit {result.returncode}."
        )


def gradle_wrapper(
    godot_dir: Path,
    task: str,
) -> None:
    """Invoke ``platform/android/java/gradlew <task>``.

    Used by the Android build for ``generateGodotEditor``,
    ``generateGodotTemplates``, ``generateGodotMonoTemplates``.
    """
    gradle_root = godot_dir / "platform" / "android" / "java"
    gradlew = gradle_root / "gradlew"
    if not gradlew.is_file():
        raise InContainerBuildError(f"gradlew missing at {gradlew}.")
    cmd: list[str] = [str(gradlew), task]
    logger.info("Running: %s (cwd=%s)", " ".join(cmd), gradle_root)
    result = subprocess.run(cmd, cwd=str(gradle_root))
    if result.returncode != 0:
        raise InContainerBuildError(
            f"gradlew {task} failed with exit {result.returncode}."
        )


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


def env_flag(name: str, *, default: bool = False) -> bool:
    """Read ``os.environ[name]`` as a build flag.

    The orchestrator passes ``CLASSICAL`` / ``MONO`` / ``STEAM`` as ``"1"``
    or ``"0"``; we honour both that contract and unset-means-default.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip() == "1"


def env_out_root(default: str = "/root/out") -> Path:
    """Return the container output root, honouring ``OUT_ROOT`` env override.

    Tests set ``OUT_ROOT=<tmp_path>`` to redirect writes away from the real
    ``/root/out``; production runs leave it unset and the default is used.
    """
    return Path(os.environ.get("OUT_ROOT") or default)


def env_mono_glue_dir(default: str = "/root/mono-glue") -> Path:
    """Return the mono glue source dir, honouring ``MONO_GLUE_DIR`` env override."""
    return Path(os.environ.get("MONO_GLUE_DIR") or default)


def env_num_cores(default: int = 1) -> int:
    """Read ``NUM_CORES`` from the env; fall back to *default* if unset/bad."""
    raw = os.environ.get("NUM_CORES")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "NUM_CORES='%s' is not an integer; defaulting to %d.", raw, default
        )
        return default
    return max(1, value)


def env_build_archs() -> set[str] | None:
    """Return the arch scope from ``GODOT_BUILD_ARCHS``, or ``None`` for all.

    The host orchestrator sets ``GODOT_BUILD_ARCHS`` to a comma-separated arch
    list when a Release run is scoped to a subset of arches (e.g. ``x86_64``
    for a fast first build); unset or empty means "build every arch this
    container supports" — the historical default. Per-platform build scripts
    intersect their arch matrix with this set, so an arch that is not in the
    set is skipped.
    """
    raw = os.environ.get("GODOT_BUILD_ARCHS")
    if not raw or not raw.strip():
        return None
    archs = {a.strip() for a in raw.split(",") if a.strip()}
    return archs or None


def parse_argv(argv: Sequence[str] | None) -> list[str]:
    """Normalise *argv* the same way every ``main(argv=None)`` does.

    Returns ``sys.argv[1:]`` when *argv* is ``None``; otherwise returns a list
    copy of *argv*. Kept here so all entry points share one form.
    """
    if argv is None:
        return list(sys.argv[1:])
    return list(argv)
