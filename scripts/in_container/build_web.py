"""In-container entry point for the Web build container.

Builds the Web editor + templates (classical) plus optional Mono templates.
Each variant outputs a ``.zip`` file containing the emscripten-linked WASM
module.

Layout:

  * 4 classical template variants — template_debug, template_release, each
    also with ``dlink_enabled=yes``.
  * 4 "nothreads" classical template variants — same matrix with
    ``threads=no``.
  * 1 classical editor with ``use_closure_compiler=yes``.

Mono on Web: not supported by Godot upstream.

  Upstream Godot rejects ``platform=web`` with ``module_mono_enabled=yes``;
  ``modules/mono/config.py`` (line 14) prints *"The 'mono' module does not
  currently support building for this platform. Aborting."* and exits 255.

  When ``MONO=1`` we therefore log an INFO note about the upstream
  constraint and skip the Mono pass entirely. ``scripts/packager.py``
  already handles the absence of web-mono artifacts gracefully, so the
  final release is clean.

Concurrency: four scons jobs run concurrently from separate clones of the
source tree to speed LTO link-time. The parallel-jobs-per-clone pattern
goes through :class:`concurrent.futures.ThreadPoolExecutor` so the
host-side log fan-out (stdout + tee'd log file) still works without
multiplexing multiple stdouts.

Env contract:

  ===========  ========================================================
  Var          Meaning
  ===========  ========================================================
  CLASSICAL    Build classical artifacts when "1".
  MONO         Accepted but treated as a no-op for web (upstream Godot
               does not support ``module_mono_enabled=yes`` on the web
               platform). Logged at INFO and skipped.
  NUM_CORES    Threads for scons. Divided by NUM_JOBS=5 so concurrent
               jobs each get ``-j${NUM_CORES/5}``.
  ===========  ========================================================
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from scripts.in_container import common

logger = logging.getLogger(__name__)


_OPTIONS: tuple[str, ...] = ("production=yes",)

_NUM_JOBS = 5

# Upstream Godot rejects mono on the web platform — see
# upstream/godot/modules/mono/config.py line 14:
#     "The 'mono' module does not currently support building for this platform.
#      Aborting."
# We surface this as an INFO log when MONO=1 and skip the Mono pass.
_WEB_MONO_SKIP_MESSAGE = (
    "Web Mono variants are not supported by Godot upstream "
    "(modules/mono/config.py rejects platform=web). "
    "Skipping Mono pass; this is the expected behaviour. "
    "Classical web templates were built normally."
)

_JOBS: tuple[tuple[str, ...], ...] = (
    ("target=template_debug",),
    ("target=template_release",),
    ("target=template_debug", "dlink_enabled=yes"),
    ("target=template_release", "dlink_enabled=yes"),
)
_JOBS_NOTHREADS: tuple[tuple[str, ...], ...] = (
    ("target=template_debug", "threads=no"),
    ("target=template_release", "threads=no"),
    ("target=template_debug", "dlink_enabled=yes", "threads=no"),
    ("target=template_release", "dlink_enabled=yes", "threads=no"),
)


def _per_job_cores(num_cores: int) -> int:
    return max(1, num_cores // _NUM_JOBS)


def _source_emsdk(env: dict[str, str]) -> dict[str, str]:
    """Source ``/root/emsdk/emsdk_env.sh`` and capture the resulting env.

    Run the script in a subshell to set EMSCRIPTEN_ROOT, PATH, etc., and
    read the resulting env back. If the script is missing (test/CI), we
    fall back to the unmodified env with a WARNING.
    """
    emsdk_env_sh = Path("/root/emsdk/emsdk_env.sh")
    if not emsdk_env_sh.is_file():
        logger.warning(
            "%s not found; emsdk env will NOT be set. Scons will likely fail.",
            emsdk_env_sh,
        )
        return env
    result = subprocess.run(
        ["bash", "-c", f"source {emsdk_env_sh} >/dev/null 2>&1 && env"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.warning(
            "sourcing %s exited %d; using unsourced env.",
            emsdk_env_sh,
            result.returncode,
        )
        return env
    sourced: dict[str, str] = dict(env)
    for line in result.stdout.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        sourced[key] = value
    return sourced


def _clone_source(src: Path, dest: Path) -> None:
    """Copy *src* to *dest* (used by parallel-job pattern)."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)


def _run_parallel_jobs(
    jobs: Sequence[tuple[str, ...]],
    *,
    clone_prefix: Path,
    base_source: Path,
    num_cores: int,
    env: dict[str, str],
) -> None:
    """Run every job in *jobs* concurrently, each from its own source clone."""
    per_job_cores = _per_job_cores(num_cores)
    clones: list[Path] = []
    for i in range(len(jobs)):
        clone = clone_prefix.parent / f"{clone_prefix.name}{i}"
        _clone_source(base_source, clone)
        clones.append(clone)

    def _job(index: int) -> None:
        clone = clones[index]
        common.run_scons(
            "platform=web",
            *_OPTIONS,
            *jobs[index],
            num_cores=per_job_cores,
            env=env,
            cwd=clone,
        )

    # One process per variant — no bounded parallelism beyond len(jobs).
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        list(ex.map(_job, range(len(jobs))))


def _copy_zips(src_bin: Path, dest: Path, *, pattern: str = "*.zip") -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if not src_bin.is_dir():
        return
    for zf in src_bin.glob(pattern):
        shutil.copy2(zf, dest / zf.name)


def main(argv: Sequence[str] | None = None) -> int:
    from scripts.console import configure_logging

    del argv
    configure_logging(verbose=False)

    num_cores = common.env_num_cores()
    classical = common.env_flag("CLASSICAL")
    mono = common.env_flag("MONO")
    out_root = common.env_out_root()
    env = dict(os.environ)
    env.setdefault("TERM", "xterm")

    try:
        env = _source_emsdk(env)
        godot_dir = common.setup_godot_source()

        if classical:
            logger.info("Starting classical build for Web...")
            per_job_cores = _per_job_cores(num_cores)

            # Threaded variants: four parallel jobs + the editor in-place.
            _run_parallel_jobs(
                _JOBS,
                clone_prefix=Path("/root/godot"),
                base_source=godot_dir,
                num_cores=num_cores,
                env=env,
            )

            # Editor pass — runs in the base godot/ dir alongside the parallel jobs.
            common.run_scons(
                "platform=web",
                *_OPTIONS,
                "target=editor",
                "use_closure_compiler=yes",
                num_cores=per_job_cores,
                env=env,
                cwd=godot_dir,
            )

            # Nothreads variants — four more parallel jobs.
            _run_parallel_jobs(
                _JOBS_NOTHREADS,
                clone_prefix=Path("/root/godot-nothreads"),
                base_source=godot_dir,
                num_cores=num_cores,
                env=env,
            )

            _copy_zips(godot_dir / "bin", out_root / "tools", pattern="*.editor*.zip")
            for i in range(len(_JOBS)):
                _copy_zips(
                    Path(f"/root/godot{i}/bin"),
                    out_root / "templates",
                )
                _copy_zips(
                    Path(f"/root/godot-nothreads{i}/bin"),
                    out_root / "templates",
                )

        if mono:
            # Upstream Godot does not support mono on web — see
            # upstream/godot/modules/mono/config.py line 14. Skip the pass
            # entirely; packager.py handles the absence gracefully.
            logger.info(_WEB_MONO_SKIP_MESSAGE)

        logger.info("Web build successful")
        return 0
    except common.InContainerBuildError as exc:
        logger.error("%s", exc)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
