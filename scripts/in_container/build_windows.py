"""In-container entry point for the Windows build container.

Builds Windows editor + templates (classical + Mono) across three archs
(x86_64, x86_32, arm64). x86 uses mingw; arm64 uses llvm-mingw. arm64 is a
first-class target — the ANGLE version the engine pins (chromium/7219, fetched
host-side by the orchestrator) resolved the old libc++ symbol clash that
previously made it best-effort.

Env contract:

  ===========  ========================================================
  Var          Meaning
  ===========  ========================================================
  CLASSICAL    Build classical artifacts when "1".
  MONO         Build mono artifacts when "1".
  STEAM        Build steam variant after classical x86 when "1".
  BUILD_NAME   Pinned to "steam" for the steam pass; restored after.
  NUM_CORES    ``scons -j``.
  ===========  ========================================================

Gaps already in bash, replicated here:

  * ``install_d3d12_sdk_windows.py`` runs before any scons pass with a 10-min
    timeout × 4 attempts; failure is fatal.
  * ``copy_and_clean_bin`` preserves ``bin/build_deps/`` so the D3D12 deps
    survive across arch cycles.
  * arm64 builds are first-class (``check=True`` like x86): a failure aborts
    the Windows build rather than being swallowed.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from scripts.in_container import common

logger = logging.getLogger(__name__)


_OPTIONS: tuple[str, ...] = (
    "production=yes",
    "accesskit_sdk_path=/root/accesskit/accesskit-c",
    "use_mingw=yes",
    "angle_libs=/root/angle",
    "winrt_path=/root/winrt",
    "d3d12=yes",
)
_OPTIONS_MONO: tuple[str, ...] = (
    "module_mono_enabled=yes",
    "module_dotnet_enabled=yes",
)
_OPTIONS_LLVM: tuple[str, ...] = (
    "use_llvm=yes",
    "mingw_prefix=/root/llvm-mingw",
)


def _scons_chain(
    godot_dir: Path,
    *,
    arch: str,
    extra: Sequence[str],
    targets: Sequence[str],
    num_cores: int,
    env: dict[str, str],
    check: bool = True,
) -> bool:
    """Run scons once per target with the shared OPTIONS+extra prefix.

    Returns True iff every invocation exits 0. With ``check=False`` we stop at
    the first failure and return False (``check=True``, the default, lets
    ``run_scons`` raise so a failure aborts the build).
    """
    for target in targets:
        rc = common.run_scons(
            "platform=windows",
            f"arch={arch}",
            *_OPTIONS,
            *extra,
            f"target={target}",
            num_cores=num_cores,
            env=env,
            cwd=godot_dir,
            check=check,
        )
        if rc != 0:
            return False
    return True


def main(argv: Sequence[str] | None = None) -> int:
    from scripts.console import configure_logging

    del argv
    configure_logging(verbose=False)

    num_cores = common.env_num_cores()
    classical = common.env_flag("CLASSICAL")
    mono = common.env_flag("MONO")
    steam = common.env_flag("STEAM")
    out_root = common.env_out_root()
    mono_glue_src = common.env_mono_glue_dir()
    env = dict(os.environ)
    env.setdefault("TERM", "xterm")

    try:
        godot_dir = common.setup_godot_source()
        bin_dir = godot_dir / "bin"

        # D3D12 SDK install — must run after tarball extract, before any scons.
        common.install_d3d12_sdk(godot_dir)

        if classical:
            logger.info("Starting classical build for Windows...")

            # x86_64
            _scons_chain(
                godot_dir,
                arch="x86_64",
                extra=(),
                targets=("editor",),
                num_cores=num_cores,
                env=env,
            )
            common.copy_and_clean_bin(bin_dir, out_root / "x86_64" / "tools")
            _scons_chain(
                godot_dir,
                arch="x86_64",
                extra=(),
                targets=("template_debug", "template_release"),
                num_cores=num_cores,
                env=env,
            )
            common.copy_and_clean_bin(bin_dir, out_root / "x86_64" / "templates")

            # x86_32
            _scons_chain(
                godot_dir,
                arch="x86_32",
                extra=(),
                targets=("editor",),
                num_cores=num_cores,
                env=env,
            )
            common.copy_and_clean_bin(bin_dir, out_root / "x86_32" / "tools")
            _scons_chain(
                godot_dir,
                arch="x86_32",
                extra=(),
                targets=("template_debug", "template_release"),
                num_cores=num_cores,
                env=env,
            )
            common.copy_and_clean_bin(bin_dir, out_root / "x86_32" / "templates")

            # arm64 (llvm-mingw). First-class since ANGLE chromium/7219 — the
            # rebuild the engine pins — resolves the old libc++ symbol clash
            # that made 6601.2 fail to link against llvm-mingw.
            _scons_chain(
                godot_dir,
                arch="arm64",
                extra=_OPTIONS_LLVM,
                targets=("editor",),
                num_cores=num_cores,
                env=env,
            )
            common.copy_and_clean_bin(bin_dir, out_root / "arm64" / "tools")
            _scons_chain(
                godot_dir,
                arch="arm64",
                extra=_OPTIONS_LLVM,
                targets=("template_debug", "template_release"),
                num_cores=num_cores,
                env=env,
            )
            common.copy_and_clean_bin(bin_dir, out_root / "arm64" / "templates")

            # Always cleanup bin/ (preserve build_deps) so Mono pass starts clean.
            common.clean_bin_preserving(bin_dir)

            # Steam build (x86_64 + x86_32 editor with steamapi=yes).
            if steam:
                build_name_save = os.environ.get("BUILD_NAME", "")
                env_steam = dict(env)
                env_steam["BUILD_NAME"] = "steam"
                _scons_chain(
                    godot_dir,
                    arch="x86_64",
                    extra=("steamapi=yes",),
                    targets=("editor",),
                    num_cores=num_cores,
                    env=env_steam,
                )
                _scons_chain(
                    godot_dir,
                    arch="x86_32",
                    extra=("steamapi=yes",),
                    targets=("editor",),
                    num_cores=num_cores,
                    env=env_steam,
                )
                common.copy_and_clean_bin(bin_dir, out_root / "steam")
                env["BUILD_NAME"] = build_name_save

        if mono:
            logger.info("Starting Mono build for Windows...")
            common.copy_mono_glue(mono_glue_src, godot_dir, include_editor=True)

            # x86_64
            _scons_chain(
                godot_dir,
                arch="x86_64",
                extra=_OPTIONS_MONO,
                targets=("editor",),
                num_cores=num_cores,
                env=env,
            )
            common.build_mono_assemblies(godot_dir, godot_platform="windows")
            common.copy_and_clean_bin(bin_dir, out_root / "x86_64" / "tools-mono")
            _scons_chain(
                godot_dir,
                arch="x86_64",
                extra=_OPTIONS_MONO,
                targets=("template_debug", "template_release"),
                num_cores=num_cores,
                env=env,
            )
            common.copy_and_clean_bin(bin_dir, out_root / "x86_64" / "templates-mono")

            # x86_32
            _scons_chain(
                godot_dir,
                arch="x86_32",
                extra=_OPTIONS_MONO,
                targets=("editor",),
                num_cores=num_cores,
                env=env,
            )
            common.build_mono_assemblies(godot_dir, godot_platform="windows")
            common.copy_and_clean_bin(bin_dir, out_root / "x86_32" / "tools-mono")
            _scons_chain(
                godot_dir,
                arch="x86_32",
                extra=_OPTIONS_MONO,
                targets=("template_debug", "template_release"),
                num_cores=num_cores,
                env=env,
            )
            common.copy_and_clean_bin(bin_dir, out_root / "x86_32" / "templates-mono")

            # arm64 Mono — best-effort, same ANGLE link clash.
            arm64_mono_ok = _scons_chain(
                godot_dir,
                arch="arm64",
                extra=tuple(_OPTIONS_MONO) + _OPTIONS_LLVM,
                targets=("editor",),
                num_cores=num_cores,
                env=env,
                check=False,
            )
            if arm64_mono_ok:
                try:
                    common.build_mono_assemblies(godot_dir, godot_platform="windows")
                    common.copy_and_clean_bin(
                        bin_dir, out_root / "arm64" / "tools-mono"
                    )
                    arm64_mono_ok = _scons_chain(
                        godot_dir,
                        arch="arm64",
                        extra=tuple(_OPTIONS_MONO) + _OPTIONS_LLVM,
                        targets=("template_debug", "template_release"),
                        num_cores=num_cores,
                        env=env,
                        check=False,
                    )
                    if arm64_mono_ok:
                        common.copy_and_clean_bin(
                            bin_dir, out_root / "arm64" / "templates-mono"
                        )
                except common.InContainerBuildError as exc:
                    arm64_mono_ok = False
                    logger.warning("arm64 Mono follow-on step failed: %s", exc)
            if not arm64_mono_ok:
                logger.warning(
                    "arm64 Mono Windows build failed (known prebuilt-ANGLE link clash); "
                    "x86_64/x86_32 Mono outputs are unaffected."
                )

            common.clean_bin_preserving(bin_dir)

        logger.info("Windows build successful")
        return 0
    except common.InContainerBuildError as exc:
        logger.error("%s", exc)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
