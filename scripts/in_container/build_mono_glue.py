"""In-container entry point for the Mono-glue stage.

Generates the Mono glue (C# bindings to Godot's GDExtension surface) and
stages it at ``/root/mono-glue/`` for the per-platform Mono passes to
consume.

Stages:

  * Adds ``${GODOT_SDK_LINUX_X86_64}/bin`` to PATH (buildroot toolchain).
  * Extracts ``/root/godot.tar.gz`` into ``/root/godot``.
  * If ``MONO=1``:
      - Logs ``dotnet --info``.
      - Builds the linuxbsd editor with mono enabled.
      - Runs ``bin/godot.linuxbsd.editor.x86_64.mono --headless
        --generate-mono-glue /root/mono-glue``.

Env contract (read from ``os.environ``):

  ===========  ===========================================================
  Var          Meaning
  ===========  ===========================================================
  MONO         Build mono glue ("1") or skip ("0"). When skipped the
               module is effectively a no-op other than source extract;
               the host orchestrator's resumability gate keeps this fast.
  NUM_CORES    Threads for scons ``-j``.
  ===========  ===========================================================

We do NOT honour CLASSICAL here — mono glue is only relevant to Mono builds.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence

from scripts.in_container import common

logger = logging.getLogger(__name__)


_GODOT_OPTIONS: tuple[str, ...] = (
    "debug_symbols=no",
    "use_static_cpp=no",
)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point invoked by ``python3 -m scripts.in_container.build_mono_glue``."""
    from scripts.console import configure_logging

    del argv  # No CLI args; env-driven.
    configure_logging(verbose=False)

    num_cores = common.env_num_cores()
    mono = common.env_flag("MONO")

    # Buildroot SDK PATH — mirrors `export PATH="${GODOT_SDK_LINUX_X86_64}/bin:${BASE_PATH}"`.
    env = dict(os.environ)
    sdk = os.environ.get("GODOT_SDK_LINUX_X86_64")
    base_path = os.environ.get("BASE_PATH", os.environ.get("PATH", ""))
    if sdk:
        env["PATH"] = f"{sdk}/bin:{base_path}"
    env.setdefault("TERM", "xterm")
    env.setdefault("DISPLAY", ":0")

    try:
        godot_dir = common.setup_godot_source()

        if not mono:
            logger.info("MONO != 1; skipping mono glue generation.")
            return 0

        logger.info("Building and generating Mono glue...")

        # Bash logs `dotnet --info` for diagnostics. Best-effort: never fail
        # the build because dotnet's CLI doesn't print.
        dotnet = shutil.which("dotnet")
        if dotnet is not None:
            subprocess.run([dotnet, "--info"], env=env, cwd=str(godot_dir), check=False)
        else:
            logger.info("dotnet not on PATH; skipping `dotnet --info` diagnostic.")

        common.run_scons(
            "platform=linuxbsd",
            "arch=x86_64",
            *_GODOT_OPTIONS,
            "target=editor",
            "module_mono_enabled=yes",
            "module_dotnet_enabled=yes",
            num_cores=num_cores,
            env=env,
            cwd=godot_dir,
        )

        mono_glue_dir = common.env_mono_glue_dir()
        if mono_glue_dir.exists():
            for child in list(mono_glue_dir.iterdir()):
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        else:
            mono_glue_dir.mkdir(parents=True, exist_ok=True)

        binary = godot_dir / "bin" / "godot.linuxbsd.editor.x86_64.mono"
        if not binary.is_file():
            raise common.InContainerBuildError(
                f"Expected mono editor binary at {binary}; scons did not produce it."
            )
        cmd = [
            str(binary),
            "--headless",
            "--generate-mono-glue",
            str(mono_glue_dir),
        ]
        logger.info("Running: %s", " ".join(cmd))
        result = subprocess.run(cmd, env=env, cwd=str(godot_dir))
        if result.returncode != 0:
            raise common.InContainerBuildError(
                f"--generate-mono-glue failed with exit {result.returncode}."
            )

        logger.info("Mono glue generated successfully")
        return 0
    except common.InContainerBuildError as exc:
        logger.error("%s", exc)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
