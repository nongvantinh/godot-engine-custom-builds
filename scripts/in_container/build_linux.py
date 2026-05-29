"""In-container entry point for the Linux build container.

Builds the Linux editor + templates (classical + optional Mono) across four
arches (x86_64, x86_32, arm64, arm32) using buildroot toolchains exposed to
the container as ``GODOT_SDK_LINUX_<ARCH>`` env vars.

Env contract:

  ============================  ===================================================
  Var                            Meaning
  ============================  ===================================================
  CLASSICAL                      Build classical artifacts when "1".
  MONO                           Build mono artifacts when "1".
  NUM_CORES                      ``scons -j``.
  GODOT_SDK_LINUX_X86_64         buildroot SDK roots; PATH is set per arch.
  GODOT_SDK_LINUX_X86_32
  GODOT_SDK_LINUX_ARM64
  GODOT_SDK_LINUX_ARM32
  BASE_PATH                      Path suffix appended after the SDK bin.
  ============================  ===================================================

Each per-arch block:

  1. Sets ``PATH=${SDK}/bin:${BASE_PATH}``.
  2. Builds editor → ``/root/out/<arch>/tools/``.
  3. Builds template_debug + template_release → ``/root/out/<arch>/templates/``.
  4. (Mono) builds editor + ``build_assemblies.py`` → ``tools-mono/``.
  5. (Mono) builds template_debug + template_release → ``templates-mono/``.

Between each block, ``bin/`` is wiped (no ``build_deps`` preservation needed
on Linux — that's a Windows-only concern).
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


_OPTIONS: tuple[str, ...] = (
    "production=yes",
    "accesskit_sdk_path=/root/accesskit/accesskit-c",
)
_OPTIONS_MONO: tuple[str, ...] = (
    "module_mono_enabled=yes",
    "module_dotnet_enabled=yes",
)

# Per-arch ordering matches bash; x86_64 first because tools-mono needs it
# extracted before build_assemblies runs.
_ARCH_SDK: tuple[tuple[str, str], ...] = (
    ("x86_64", "GODOT_SDK_LINUX_X86_64"),
    ("x86_32", "GODOT_SDK_LINUX_X86_32"),
    ("arm64", "GODOT_SDK_LINUX_ARM64"),
    ("arm32", "GODOT_SDK_LINUX_ARM32"),
)


def _arch_env(arch: str, sdk_var: str) -> dict[str, str]:
    """Return an env dict with PATH set to the buildroot SDK for *arch*."""
    env = dict(os.environ)
    sdk = os.environ.get(sdk_var)
    base_path = os.environ.get("BASE_PATH", os.environ.get("PATH", ""))
    if sdk:
        env["PATH"] = f"{sdk}/bin:{base_path}"
    else:
        logger.warning(
            "%s is not set; falling back to host PATH for arch %s.", sdk_var, arch
        )
    env.setdefault("TERM", "xterm")
    return env


def _copy_bin_and_clean(godot_dir: Path, dest: Path) -> None:
    """``mkdir -p dest && cp -rvp bin/* dest && rm -rf bin`` equivalent."""
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


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    num_cores = common.env_num_cores()
    classical = common.env_flag("CLASSICAL")
    mono = common.env_flag("MONO")
    out_root = common.env_out_root()
    mono_glue_src = common.env_mono_glue_dir()

    try:
        godot_dir = common.setup_godot_source()

        if classical:
            logger.info("Starting classical build for Linux...")
            for arch, sdk_var in _ARCH_SDK:
                env = _arch_env(arch, sdk_var)

                common.run_scons(
                    "platform=linuxbsd",
                    f"arch={arch}",
                    *_OPTIONS,
                    "target=editor",
                    num_cores=num_cores,
                    env=env,
                    cwd=godot_dir,
                )
                _copy_bin_and_clean(godot_dir, out_root / arch / "tools")

                common.run_scons(
                    "platform=linuxbsd",
                    f"arch={arch}",
                    *_OPTIONS,
                    "target=template_debug",
                    num_cores=num_cores,
                    env=env,
                    cwd=godot_dir,
                )
                common.run_scons(
                    "platform=linuxbsd",
                    f"arch={arch}",
                    *_OPTIONS,
                    "target=template_release",
                    num_cores=num_cores,
                    env=env,
                    cwd=godot_dir,
                )
                _copy_bin_and_clean(godot_dir, out_root / arch / "templates")

        if mono:
            logger.info("Starting Mono build for Linux...")
            common.copy_mono_glue(mono_glue_src, godot_dir, include_editor=True)

            for arch, sdk_var in _ARCH_SDK:
                env = _arch_env(arch, sdk_var)

                common.run_scons(
                    "platform=linuxbsd",
                    f"arch={arch}",
                    *_OPTIONS,
                    *_OPTIONS_MONO,
                    "target=editor",
                    num_cores=num_cores,
                    env=env,
                    cwd=godot_dir,
                )
                common.build_mono_assemblies(godot_dir, godot_platform="linuxbsd")
                _copy_bin_and_clean(godot_dir, out_root / arch / "tools-mono")

                common.run_scons(
                    "platform=linuxbsd",
                    f"arch={arch}",
                    *_OPTIONS,
                    *_OPTIONS_MONO,
                    "target=template_debug",
                    num_cores=num_cores,
                    env=env,
                    cwd=godot_dir,
                )
                common.run_scons(
                    "platform=linuxbsd",
                    f"arch={arch}",
                    *_OPTIONS,
                    *_OPTIONS_MONO,
                    "target=template_release",
                    num_cores=num_cores,
                    env=env,
                    cwd=godot_dir,
                )
                _copy_bin_and_clean(godot_dir, out_root / arch / "templates-mono")

        logger.info("Linux build successful")
        return 0
    except common.InContainerBuildError as exc:
        logger.error("%s", exc)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
