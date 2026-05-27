#!/usr/bin/env python3
"""build-godot.py — Portable Godot engine build orchestrator.

Usage
-----
    uv run python build-godot.py build --platform linux --target editor
    uv run python build-godot.py containers --type linux --version 4.7
    uv run python build-godot.py --help

Exit codes
----------
    0  Build completed successfully.
    1  Configuration error (missing key, bad value, missing env var).
    2  Platform not supported or container image not found in config.
    3  Docker unavailable and local fallback is not possible.
    4  Build subprocess exited with non-zero status.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Python version guard — must be before any stdlib-3.11 imports.
# ---------------------------------------------------------------------------
if sys.version_info < (3, 11):
    sys.exit(
        "build-godot.py requires Python 3.11 or newer.\n"
        f"Current interpreter: {sys.version}\n"
        "Use 'uv run python build-godot.py' to get the correct version."
    )

# ---------------------------------------------------------------------------
# Project-local imports (after version guard so we get a clean error first).
# ---------------------------------------------------------------------------
from scripts.config import ConfigError, get_platform_config, load_config
from scripts.container_builder import build_and_push
from scripts.docker_helper import BuildError, DockerUnavailableError, ensure_docker, login, pull_image, run_build
from scripts.patcher import apply_patches

# ---------------------------------------------------------------------------
# Supported platforms
# ---------------------------------------------------------------------------
_SUPPORTED_PLATFORMS = {"linux", "windows", "android", "web"}

# Platforms that require Docker (no local fallback available).
_DOCKER_REQUIRED_PLATFORMS = {"windows", "android", "web"}

# Default Godot fork slug — operators can override via --godot-repo.
_DEFAULT_GODOT_REPO = "nongvantinh/godot"

# Official Godot slugs that resolve to the upstream source.
_OFFICIAL_REPO_SLUGS = {"official", "godotengine/godot"}

# Path to the upstream/godot submodule relative to this script.
_SCRIPT_DIR = Path(__file__).parent
_UPSTREAM_GODOT = _SCRIPT_DIR / "upstream" / "godot"
_PATCHES_DIR = _SCRIPT_DIR / "patches"


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build-godot.py",
        description="Portable Godot engine build orchestrator.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--version",
        action="version",
        version="build-godot.py 1.0.0",
    )

    sub = parser.add_subparsers(dest="command", metavar="<sub-command>")
    sub.required = True

    # ------------------------------------------------------------------
    # build sub-command
    # ------------------------------------------------------------------
    build_p = sub.add_parser(
        "build",
        help="Build Godot for one or more target platforms.",
        description="Build Godot using Docker containers (or local SCons on Linux).",
    )
    build_p.add_argument(
        "--platform",
        required=True,
        metavar="PLATFORM[,PLATFORM...]",
        help=(
            "Target platform(s), comma-separated. "
            "Supported: linux, windows, android, web."
        ),
    )
    build_p.add_argument(
        "--target",
        default="editor",
        metavar="TARGET",
        help=(
            "SCons build target.  Typical values: editor, templates, "
            "template_debug, template_release.  (default: editor)"
        ),
    )
    build_p.add_argument(
        "--godot-repo",
        default=_DEFAULT_GODOT_REPO,
        metavar="SLUG",
        help=(
            f"GitHub slug of the Godot source repo. "
            f"Pass 'official' or 'godotengine/godot' for the upstream source. "
            f"(default: {_DEFAULT_GODOT_REPO})"
        ),
    )
    build_p.add_argument(
        "--config",
        default="./config.toml",
        metavar="PATH",
        help="Path to the TOML configuration file.  (default: ./config.toml)",
    )
    build_p.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose (DEBUG-level) logging.",
    )
    build_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print Docker commands that would be run without executing them.",
    )

    # ------------------------------------------------------------------
    # containers sub-command
    # ------------------------------------------------------------------
    containers_p = sub.add_parser(
        "containers",
        help="Build (and optionally push) Godot Docker container images.",
        description=(
            "Build Godot container images from the Dockerfiles in the "
            "containers/ directory, then optionally push them to GHCR."
        ),
    )
    containers_p.add_argument(
        "--type",
        required=True,
        metavar="TYPE[,TYPE...]",
        help=(
            "Container type(s), comma-separated. "
            "Supported: base, linux, windows, android, web, all."
        ),
    )
    containers_p.add_argument(
        "--version",
        default=None,
        metavar="VERSION",
        help=(
            "Image version tag, e.g. 4.7. "
            "Defaults to 'godot_version' from config.toml."
        ),
    )
    containers_p.add_argument(
        "--push",
        action="store_true",
        help="Push images to GHCR after building (requires GHCR_PAT env var).",
    )
    containers_p.add_argument(
        "--config",
        default="./config.toml",
        metavar="PATH",
        help="Path to the TOML configuration file.  (default: ./config.toml)",
    )
    containers_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print Docker commands without executing them.",
    )

    return parser


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        format="%(levelname)s: %(message)s",
        level=level,
    )


# ---------------------------------------------------------------------------
# Source-directory resolution
# ---------------------------------------------------------------------------


def _resolve_source_dir(godot_repo: str) -> Path:
    """Return the local path to the Godot source directory.

    If *godot_repo* is the official slug, we fall back to `upstream/godot/`
    if it exists; otherwise we expect the caller to provide a cloned copy.

    For the fork default (``nongvantinh/godot``) we use the submodule
    directly when it is initialised.
    """
    if godot_repo in _OFFICIAL_REPO_SLUGS:
        logging.info("Using official Godot source.")
        # Official source: use upstream/godot submodule if initialised.
        if _UPSTREAM_GODOT.is_dir() and any(_UPSTREAM_GODOT.iterdir()):
            return _UPSTREAM_GODOT
        logging.warning(
            "upstream/godot/ submodule is not initialised. "
            "Run: git submodule update --init --recursive"
        )
        # Return path anyway; the build will fail with a clear Docker error.
        return _UPSTREAM_GODOT

    # Fork or custom slug — use upstream/godot submodule.
    if _UPSTREAM_GODOT.is_dir() and any(_UPSTREAM_GODOT.iterdir()):
        logging.info("Using Godot source from upstream/godot/ submodule (%s).", godot_repo)
        return _UPSTREAM_GODOT

    logging.warning(
        "upstream/godot/ submodule is not initialised. "
        "Run: git submodule update --init --recursive"
    )
    return _UPSTREAM_GODOT


# ---------------------------------------------------------------------------
# Build dispatch
# ---------------------------------------------------------------------------


def _build_platform(
    platform: str,
    target: str,
    config: dict,
    source_dir: Path,
    output_dir: Path,
    dry_run: bool,
) -> int:
    """Dispatch the build for a single *platform*.

    Returns
    -------
    int
        Exit code: 0 = success, 2 = unsupported/missing config, 3 = Docker
        unavailable (no fallback), 4 = build process failed.
    """
    logger = logging.getLogger(__name__)

    if platform not in _SUPPORTED_PLATFORMS:
        logger.error(
            "Platform '%s' is not supported. Supported platforms: %s",
            platform,
            ", ".join(sorted(_SUPPORTED_PLATFORMS)),
        )
        return 2

    platform_cfg = get_platform_config(config, platform)
    if platform_cfg is None:
        logger.error(
            "No [[platforms]] entry found for '%s' in config. "
            "Add a [[platforms]] entry with name = \"%s\".",
            platform,
            platform,
        )
        return 2

    image = platform_cfg["image"]
    scons_flags = platform_cfg["scons_flags"]

    # Merge global extra_flags.
    extra_flags = config.get("scons", {}).get("extra_flags", "").strip()
    use_lto = config.get("scons", {}).get("use_lto", False)
    lto_flag = "use_lto=yes" if use_lto else ""

    full_flags = " ".join(
        f for f in [scons_flags, f"target={target}", lto_flag, extra_flags] if f
    )

    logger.info("Building platform=%s  target=%s  image=%s", platform, target, image)
    logger.debug("SCons flags: %s", full_flags)

    output_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Docker-based build
    # ------------------------------------------------------------------
    docker_ok = True
    try:
        ensure_docker()
    except DockerUnavailableError as exc:
        docker_ok = False
        if platform in _DOCKER_REQUIRED_PLATFORMS:
            logger.error(
                "Docker is required for platform '%s' but is unavailable: %s",
                platform,
                exc,
            )
            return 3
        # Linux: fall through to local SCons fallback.
        logger.warning(
            "Docker unavailable for platform '%s': %s\n"
            "Attempting local SCons fallback.",
            platform,
            exc,
        )

    if docker_ok:
        registry = config["registry"]
        username = config["username"]
        try:
            login(registry, username, dry_run=dry_run)
        except ConfigError as exc:
            logger.error("Registry login failed: %s", exc)
            return 1

        pull_image(image, dry_run=dry_run)
        rc = run_build(
            image=image,
            scons_flags=full_flags,
            source_dir=str(source_dir.resolve()),
            output_dir=str(output_dir.resolve()),
            dry_run=dry_run,
        )
        if rc != 0:
            logger.error("Build exited with code %d.", rc)
            return 4
        return 0

    # ------------------------------------------------------------------
    # Local SCons fallback (Linux only, when Docker is unavailable)
    # ------------------------------------------------------------------
    if shutil.which("scons") is None:
        logger.error(
            "Neither Docker nor local 'scons' is available. "
            "Install one of them and try again."
        )
        return 3

    cmd = ["scons"] + full_flags.split()
    logger.info("Running local SCons build: %s", " ".join(cmd))
    if dry_run:
        logger.info("[dry-run] Would run: %s", " ".join(cmd))
        return 0
    result = subprocess.run(cmd, cwd=str(source_dir))
    if result.returncode != 0:
        logger.error("SCons build exited with code %d.", result.returncode)
        return 4
    return 0


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def cmd_containers(args: argparse.Namespace) -> int:
    """Handle the ``containers`` sub-command."""
    _configure_logging(False)
    logger = logging.getLogger(__name__)

    # Load and validate config.
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        logger.error("%s", exc)
        return 1

    registry: str = config["registry"]
    username: str = config["username"]

    # Resolve version: CLI flag takes priority, then config default.
    version: str = args.version if args.version else config["godot_version"]

    # Parse type list.
    types: list[str] = [t.strip().lower() for t in args.type.split(",") if t.strip()]
    if not types:
        logger.error("No container types specified. Use --type base,linux,...")
        return 1

    containers_dir: Path = _SCRIPT_DIR / "containers"

    return build_and_push(
        types=types,
        version=version,
        containers_dir=containers_dir,
        registry=registry,
        username=username,
        push=args.push,
        dry_run=args.dry_run,
    )


def cmd_build(args: argparse.Namespace) -> int:
    """Handle the ``build`` sub-command."""
    _configure_logging(args.verbose)
    logger = logging.getLogger(__name__)

    # Load and validate config.
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        logger.error("%s", exc)
        return 1

    # Parse platform list.
    platforms = [p.strip().lower() for p in args.platform.split(",") if p.strip()]
    if not platforms:
        logger.error("No platforms specified. Use --platform linux,windows,...")
        return 1

    # Resolve source directory.
    source_dir = _resolve_source_dir(args.godot_repo)
    output_dir = _SCRIPT_DIR / "output"

    # Apply patches before building.
    try:
        apply_patches(str(_PATCHES_DIR), str(source_dir))
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to apply patches: %s", exc)
        return 4

    # Dispatch builds — stop on first failure.
    for platform in platforms:
        logger.info("--- Starting build for platform: %s ---", platform)
        try:
            rc = _build_platform(
                platform=platform,
                target=args.target,
                config=config,
                source_dir=source_dir,
                output_dir=output_dir / platform,
                dry_run=args.dry_run,
            )
        except BuildError as exc:
            logger.error("%s", exc)
            return 4
        if rc != 0:
            return rc
        logger.info("--- Build for platform '%s' completed. ---", platform)

    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "build":
        return cmd_build(args)

    if args.command == "containers":
        return cmd_containers(args)

    # Should never reach here because sub.required = True.
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
