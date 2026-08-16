"""In-container entry point for the macOS build container.

Builds macOS editor + templates (classical + Mono) for x86_64 and arm64,
then lipo-merges to a universal binary.

Env contract:

  ===============  ====================================================
  Var               Meaning
  ===============  ====================================================
  CLASSICAL         Build classical artifacts when "1".
  MONO              Build mono artifacts when "1".
  NUM_CORES         ``scons -j``.
  SWIFT_VERSION     Override Swift toolchain (default: 6.2.1, matches
                    macOS 26.1 SDK bundled Swift).
  ===============  ====================================================

osxcross_sdk is hard-coded to darwin25.1 to match the macOS 26.1 SDK
bundled in the Apple image chain. Swift install is delegated to
:func:`common.swiftly_install`; the iOS/macOS images pre-install Swift
6.2.1 at image-build time, so the call short-circuits in the happy path.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from scripts.in_container import common

logger = logging.getLogger(__name__)


_OSXCROSS_SDK = "darwin25.1"
_DEFAULT_SWIFT_VERSION = "6.2.1"
_OPTIONS_MONO: tuple[str, ...] = (
    "module_mono_enabled=yes",
    "module_dotnet_enabled=yes",
)


def _options(swift_frontend: Path) -> tuple[str, ...]:
    return (
        f"osxcross_sdk={_OSXCROSS_SDK}",
        "production=yes",
        "use_volk=no",
        "vulkan_sdk_path=/root/moltenvk",
        "angle_libs=/root/angle",
        "accesskit_sdk_path=/root/accesskit/accesskit-c",
        f"SWIFT_FRONTEND={swift_frontend}",
    )


def _lipo(godot_dir: Path, *names: str) -> None:
    """Run ``lipo -create <names> -output <last>`` in *godot_dir*/bin."""
    lipo = shutil.which("lipo") or "lipo"
    bin_dir = godot_dir / "bin"
    inputs = [str(bin_dir / name) for name in names[:-1]]
    output = str(bin_dir / names[-1])
    cmd = [lipo, "-create", *inputs, "-output", output]
    logger.info("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise common.InContainerBuildError(
            f"lipo failed (rc={result.returncode}): {' '.join(cmd)}"
        )


def _copy_bin_clean(godot_dir: Path, dest: Path) -> None:
    bin_dir = godot_dir / "bin"
    dest.mkdir(parents=True, exist_ok=True)
    if bin_dir.is_dir():
        for child in list(bin_dir.iterdir()):
            target = dest / child.name
            if child.is_dir():
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(child, target)
            else:
                shutil.copy2(child, target)
        shutil.rmtree(bin_dir)


def _require_pre_installed_swift_toolchain(version: str) -> None:
    """Raise a clear, actionable error when the default Swift toolchain is missing.

    The ``godot-osx`` image pre-installs Swift ``_DEFAULT_SWIFT_VERSION`` at
    image-build time with full GPG verification. If that artifact is absent
    the image was not built (or was built against a different Xcode bundle),
    so fail loud with operator-facing remediation steps instead of falling
    through to a runtime swiftly install that would also fail.
    """
    if version != _DEFAULT_SWIFT_VERSION:
        # An override is the operator's call; let swiftly_install handle it.
        return
    swift_frontend = Path(
        f"/root/.local/share/swiftly/toolchains/{version}/usr/bin/swift-frontend"
    )
    if not swift_frontend.is_file():
        raise common.InContainerBuildError(
            f"Swift {version} toolchain not pre-installed in godot-osx image "
            f"(expected {swift_frontend}). Rebuild the godot-osx image with "
            f"a valid containers/files/Xcode_<ver>.xip and try again."
        )


def main(argv: Sequence[str] | None = None) -> int:
    from scripts.console import configure_logging

    del argv
    configure_logging(verbose=False)

    num_cores = common.env_num_cores()
    classical = common.env_flag("CLASSICAL")
    mono = common.env_flag("MONO")
    swift_version = os.environ.get("SWIFT_VERSION", _DEFAULT_SWIFT_VERSION)
    out_root = common.env_out_root()
    mono_glue_src = common.env_mono_glue_dir()
    env = dict(os.environ)
    env.setdefault("TERM", "xterm")

    try:
        _require_pre_installed_swift_toolchain(swift_version)
        toolchain = common.swiftly_install(swift_version)
        swift_frontend = toolchain / "usr" / "bin" / "swift-frontend"
        options = _options(swift_frontend)

        godot_dir = common.setup_godot_source()

        if mono:
            logger.info("Starting Mono build for macOS...")
            common.copy_mono_glue(mono_glue_src, godot_dir, include_editor=True)

            common.run_scons(
                "platform=macos",
                *options,
                *_OPTIONS_MONO,
                "arch=x86_64",
                "target=editor",
                num_cores=num_cores,
                env=env,
                cwd=godot_dir,
            )
            common.run_scons(
                "platform=macos",
                *options,
                *_OPTIONS_MONO,
                "arch=arm64",
                "target=editor",
                num_cores=num_cores,
                env=env,
                cwd=godot_dir,
            )
            _lipo(
                godot_dir,
                "godot.macos.editor.x86_64.mono",
                "godot.macos.editor.arm64.mono",
                "godot.macos.editor.universal.mono",
            )
            common.build_mono_assemblies(godot_dir, godot_platform="macos")
            _copy_bin_clean(godot_dir, out_root / "tools-mono")

            for target in ("template_debug", "template_release"):
                common.run_scons(
                    "platform=macos",
                    *options,
                    *_OPTIONS_MONO,
                    "arch=x86_64",
                    f"target={target}",
                    num_cores=num_cores,
                    env=env,
                    cwd=godot_dir,
                )
                common.run_scons(
                    "platform=macos",
                    *options,
                    *_OPTIONS_MONO,
                    "arch=arm64",
                    f"target={target}",
                    num_cores=num_cores,
                    env=env,
                    cwd=godot_dir,
                )
                _lipo(
                    godot_dir,
                    f"godot.macos.{target}.x86_64.mono",
                    f"godot.macos.{target}.arm64.mono",
                    f"godot.macos.{target}.universal.mono",
                )
            _copy_bin_clean(godot_dir, out_root / "templates-mono")

        if mono and classical:
            logger.info("Restarting from clean tarball for classical pass...")
            shutil.rmtree(godot_dir)
            godot_dir = common.setup_godot_source()

        if classical:
            logger.info("Starting classical build for macOS...")
            common.run_scons(
                "platform=macos",
                *options,
                "arch=x86_64",
                "target=editor",
                num_cores=num_cores,
                env=env,
                cwd=godot_dir,
            )
            common.run_scons(
                "platform=macos",
                *options,
                "arch=arm64",
                "target=editor",
                num_cores=num_cores,
                env=env,
                cwd=godot_dir,
            )
            _lipo(
                godot_dir,
                "godot.macos.editor.x86_64",
                "godot.macos.editor.arm64",
                "godot.macos.editor.universal",
            )
            _copy_bin_clean(godot_dir, out_root / "tools")

            for target in ("template_debug", "template_release"):
                common.run_scons(
                    "platform=macos",
                    *options,
                    "arch=x86_64",
                    f"target={target}",
                    num_cores=num_cores,
                    env=env,
                    cwd=godot_dir,
                )
                common.run_scons(
                    "platform=macos",
                    *options,
                    "arch=arm64",
                    f"target={target}",
                    num_cores=num_cores,
                    env=env,
                    cwd=godot_dir,
                )
                _lipo(
                    godot_dir,
                    f"godot.macos.{target}.x86_64",
                    f"godot.macos.{target}.arm64",
                    f"godot.macos.{target}.universal",
                )
            _copy_bin_clean(godot_dir, out_root / "templates")

        logger.info("macOS build successful")
        return 0
    except common.InContainerBuildError as exc:
        logger.error("%s", exc)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
