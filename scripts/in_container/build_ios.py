"""In-container entry point for the iOS build container.

Builds iOS templates (classical + Mono) for arm64 device and x86_64
simulator. arm64 simulator is disabled (cctools-port + current LLVM
incompatibility; see https://github.com/godotengine/build-containers/pull/85).

Env contract:

  ===============  =====================================================
  Var               Meaning
  ===============  =====================================================
  CLASSICAL         Build classical artifacts when "1".
  MONO              Build mono artifacts when "1".
  NUM_CORES         ``scons -j``.
  SWIFT_VERSION     Override Swift toolchain (default 6.2.1, matches
                    Xcode 26.1.1 SDK bundled Swift).
  ===============  =====================================================

Hard-coded SDK paths are tied to the godot-ios image contents (Xcode 26.1.1
/ iOS 26.1 SDK + the cctools-port toolchains at /root/ioscross/<arch>/).
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from scripts.in_container import common

logger = logging.getLogger(__name__)


_IOS_SDK = "26.1"
_DEFAULT_SWIFT_VERSION = "6.2.1"

_IOS_DEVICE: tuple[str, ...] = (
    f"IOS_SDK_PATH=/root/Xcode.app/Contents/Developer/Platforms/iPhoneOS.platform/"
    f"Developer/SDKs/iPhoneOS{_IOS_SDK}.sdk",
)
_IOS_SIMULATOR: tuple[str, ...] = (
    f"IOS_SDK_PATH=/root/Xcode.app/Contents/Developer/Platforms/iPhoneSimulator.platform/"
    f"Developer/SDKs/iPhoneSimulator{_IOS_SDK}.sdk",
    "simulator=yes",
)
_APPLE_TARGET_ARM64: tuple[str, ...] = (
    "APPLE_TOOLCHAIN_PATH=/root/ioscross/arm64",
    "apple_target_triple=arm-apple-darwin11-",
)
_APPLE_TARGET_X86_64: tuple[str, ...] = (
    "APPLE_TOOLCHAIN_PATH=/root/ioscross/x86_64",
    "apple_target_triple=x86_64-apple-darwin11-",
)

_OPTIONS_MONO: tuple[str, ...] = (
    "module_mono_enabled=yes",
    "module_dotnet_enabled=yes",
)


def _options(swift_frontend: Path) -> tuple[str, ...]:
    return (
        "production=yes",
        "use_lto=no",  # iOS deploy regression w/ LTO
        f"SWIFT_FRONTEND={swift_frontend}",
    )


# Per-arch + per-target file copy table (release first, then debug).
_TEMPLATE_OUTPUTS: tuple[tuple[str, str], ...] = (
    ("libgodot.ios.template_release.arm64.a", "libgodot.ios.a"),
    ("libgodot.ios.template_debug.arm64.a", "libgodot.ios.debug.a"),
    ("libgodot_camera.ios.template_release.arm64.a", "libgodot_camera.ios.a"),
    ("libgodot_camera.ios.template_debug.arm64.a", "libgodot_camera.ios.debug.a"),
    (
        "libgodot.ios.template_release.x86_64.simulator.a",
        "libgodot.ios.simulator.a",
    ),
    (
        "libgodot.ios.template_debug.x86_64.simulator.a",
        "libgodot.ios.debug.simulator.a",
    ),
    (
        "libgodot_camera.ios.template_release.x86_64.simulator.a",
        "libgodot_camera.ios.simulator.a",
    ),
    (
        "libgodot_camera.ios.template_debug.x86_64.simulator.a",
        "libgodot_camera.ios.debug.simulator.a",
    ),
)


def _copy_templates(godot_dir: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    bin_dir = godot_dir / "bin"
    for src_name, dest_name in _TEMPLATE_OUTPUTS:
        src = bin_dir / src_name
        if not src.is_file():
            logger.warning("Expected iOS artifact missing: %s", src)
            continue
        shutil.copy2(src, dest / dest_name)


def _require_pre_installed_swift_toolchain(version: str) -> None:
    """Raise a clear, actionable error when the default Swift toolchain is missing.

    The ``godot-ios`` image (which derives from ``godot-osx``) pre-installs
    Swift ``_DEFAULT_SWIFT_VERSION`` at image-build time with full GPG
    verification. If that artifact is absent the image was not built (or was
    built against a different Xcode bundle), so fail loud with operator-facing
    remediation steps instead of falling through to a runtime swiftly install.
    """
    if version != _DEFAULT_SWIFT_VERSION:
        # An override is the operator's call; let swiftly_install handle it.
        return
    swift_frontend = Path(
        f"/root/.local/share/swiftly/toolchains/{version}/usr/bin/swift-frontend"
    )
    if not swift_frontend.is_file():
        raise common.InContainerBuildError(
            f"Swift {version} toolchain not pre-installed in godot-ios image "
            f"(expected {swift_frontend}). Rebuild the godot-osx/godot-ios "
            f"images with a valid containers/files/Xcode_<ver>.xip and try again."
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
            logger.info("Starting Mono build for iOS...")
            common.copy_mono_glue(mono_glue_src, godot_dir, include_editor=False)

            # arm64 device
            for target in ("template_debug", "template_release"):
                common.run_scons(
                    "platform=ios",
                    *options,
                    *_OPTIONS_MONO,
                    "arch=arm64",
                    f"target={target}",
                    *_IOS_DEVICE,
                    *_APPLE_TARGET_ARM64,
                    num_cores=num_cores,
                    env=env,
                    cwd=godot_dir,
                )
            # x86_64 simulator
            for target in ("template_debug", "template_release"):
                common.run_scons(
                    "platform=ios",
                    *options,
                    *_OPTIONS_MONO,
                    "arch=x86_64",
                    f"target={target}",
                    *_IOS_SIMULATOR,
                    *_APPLE_TARGET_X86_64,
                    num_cores=num_cores,
                    env=env,
                    cwd=godot_dir,
                )
            _copy_templates(godot_dir, out_root / "templates-mono")

        if mono and classical:
            logger.info("Restarting from clean tarball for classical pass...")
            shutil.rmtree(godot_dir)
            godot_dir = common.setup_godot_source()

        if classical:
            logger.info("Starting classical build for iOS...")
            # arm64 device
            for target in ("template_debug", "template_release"):
                common.run_scons(
                    "platform=ios",
                    *options,
                    "arch=arm64",
                    f"target={target}",
                    *_IOS_DEVICE,
                    *_APPLE_TARGET_ARM64,
                    num_cores=num_cores,
                    env=env,
                    cwd=godot_dir,
                )
            # arm64 simulator disabled — cctools-port + current LLVM
            # incompatibility (see godotengine/build-containers#85).
            # x86_64 simulator
            for target in ("template_debug", "template_release"):
                common.run_scons(
                    "platform=ios",
                    *options,
                    "arch=x86_64",
                    f"target={target}",
                    *_IOS_SIMULATOR,
                    *_APPLE_TARGET_X86_64,
                    num_cores=num_cores,
                    env=env,
                    cwd=godot_dir,
                )
            _copy_templates(godot_dir, out_root / "templates")

        logger.info("iOS build successful")
        return 0
    except common.InContainerBuildError as exc:
        logger.error("%s", exc)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
