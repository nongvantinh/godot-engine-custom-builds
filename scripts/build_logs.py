"""Run-scoped build log directories and per-invocation log path helpers.

Every release/build run allocates ``build-godot-and-templates/logs/<run-id>/``:

  logs/<run-id>/release.log          orchestrator stdout (release sub-command)
  logs/<run-id>/mono-glue/container.log
  logs/<run-id>/<platform>/container.log
  logs/<run-id>/mono-glue/scons.<target>.log     mono glue scons pass
  logs/<run-id>/<platform>/<flavor>/<arch>.<target>.log   each scons pass
  logs/<run-id>/<platform>/<flavor>/gradle.<task>.log     Android gradle steps

``logs/latest`` symlinks to the most recent run id.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def allocate_run_logs_dir(basedir: Path, *, dry_run: bool = False) -> Path:
    """Create ``basedir/logs/<timestamp>/`` and refresh ``basedir/logs/latest``."""
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = basedir / "logs" / run_id
    latest = basedir / "logs" / "latest"
    if dry_run:
        logger.info("[dry-run] Would create run log dir %s", run_dir)
        return run_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(run_id, target_is_directory=True)
    logger.info("Run logs: %s (latest -> %s)", run_dir, latest)
    return run_dir


def platform_container_log(run_logs_dir: Path, platform: str) -> Path:
    return run_logs_dir / platform / "container.log"


def mono_glue_container_log(run_logs_dir: Path) -> Path:
    return run_logs_dir / "mono-glue" / "container.log"


def attach_release_file_handler(log_file: Path) -> logging.Handler:
    """Mirror root logging to *log_file* for the release/build top-level transcript."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(message)s")
        if not sys.stdout.isatty()
        else logging.Formatter("%(asctime)s %(levelname)-8s %(message)s", "%H:%M:%S")
    )
    logging.getLogger().addHandler(handler)
    return handler


def _flag_value(args: tuple[str, ...], prefix: str, default: str = "unknown") -> str:
    needle = f"{prefix}="
    for token in args:
        if token.startswith(needle):
            return token[len(needle) :]
    return default


def scons_log_path(args: tuple[str, ...]) -> Path | None:
    """Return the per-arch scons log path when ``GODOT_LOG_DIR`` is set."""
    root = os.environ.get("GODOT_LOG_DIR")
    if not root:
        return None
    platform = os.environ.get("GODOT_BUILD_PLATFORM", "unknown")
    target = _flag_value(args, "target", default="build")
    if platform == "mono-glue":
        return Path(root) / "mono-glue" / f"scons.{target}.log"
    flavor = "mono" if "module_mono_enabled=yes" in args else "classical"
    arch = _flag_value(args, "arch")
    return Path(root) / platform / flavor / f"{arch}.{target}.log"


def gradle_log_path(task: str) -> Path | None:
    """Return the gradle task log path when ``GODOT_LOG_DIR`` is set."""
    root = os.environ.get("GODOT_LOG_DIR")
    if not root:
        return None
    platform = os.environ.get("GODOT_BUILD_PLATFORM", "android")
    flavor = os.environ.get("GODOT_BUILD_FLAVOR", "classical")
    safe_task = task.replace("/", "_")
    return Path(root) / platform / flavor / f"gradle.{safe_task}.log"
