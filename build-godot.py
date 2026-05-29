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
    4  Build/package/upload subprocess exited with non-zero status.
    5  Release publish failed (gh auth missing, tag absent, asset conflict).
"""

from __future__ import annotations

import argparse
import logging
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
from scripts.config import (
    DEFAULT_GIT_BRANCH,
    ConfigError,
    get_build_config,
    get_platform_archs,
    get_platform_config,
    get_release_config,
    get_scons_config,
    load_config,
)
from scripts.container_builder import build_and_push, extract_apple_sdks
from scripts.docker_helper import (
    BuildError,
    DockerUnavailableError,
    ensure_docker,
    login,
    pull_image,
    run_build,
)
from scripts.orchestrator import (
    PublishError,
    collect_release_assets,
    dispatch_build,
    package_release,
    publish_release,
)
from scripts.patcher import apply_patches
from scripts.scons_args import SconsDerivationError, derive_invocations

# ---------------------------------------------------------------------------
# Supported platforms
# ---------------------------------------------------------------------------
_SUPPORTED_PLATFORMS = {"linux", "windows", "android", "web", "macos", "ios"}

# Platforms that require Docker (no local fallback available).
_DOCKER_REQUIRED_PLATFORMS = {"windows", "android", "web", "macos", "ios"}

# Default Godot fork slug — operators can override via --godot-repo.
_DEFAULT_GODOT_REPO = "nongvantinh/godot"

# Working directory the host orchestrator + packager operate on (deps/, out/,
# releases/, tarball staging).
_BUILD_AND_TEMPLATES_DIR = Path(__file__).parent / "build-godot-and-templates"

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
            "Target platform(s), comma-separated, or 'all'. "
            "Supported: linux, windows, android, web, macos, ios, all."
        ),
    )
    build_p.add_argument(
        "--flavor",
        default=None,
        metavar="FLAVOR[,FLAVOR...]",
        help=(
            "Flavor(s), comma-separated: release, debug, release_debug. "
            "Replaces --target for flavor selection. "
            "(default: from [build].flavors)"
        ),
    )
    build_p.add_argument(
        "--kind",
        default=None,
        metavar="KIND[,KIND...]",
        help=(
            "Artifact kind(s), comma-separated: editor, templates. "
            "(default: from [build].kinds)"
        ),
    )
    build_p.add_argument(
        "--mono",
        default=None,
        choices=["on", "off", "both"],
        help=("Mono variant: on, off, or both. " "(default: from [build].mono)"),
    )
    build_p.add_argument(
        "--arch",
        default=None,
        metavar="ARCH[,ARCH...]",
        help=(
            "Restrict the arch matrix, comma-separated. "
            "(default: from [[platforms]].archs)"
        ),
    )
    build_p.add_argument(
        "--jobs",
        type=int,
        default=None,
        metavar="N",
        help=(
            "SCons -j parallelism. "
            "(default: from [build].build_jobs = nproc - 2, leaving 2 cores "
            "for the system)"
        ),
    )
    build_p.add_argument(
        "--target",
        default=None,
        metavar="TARGET",
        help=(
            "DEPRECATED escape hatch. When supplied it bypasses "
            "flavor/kind/mono derivation and is passed to SCons verbatim."
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
        required=False,
        default=None,
        metavar="TYPE[,TYPE...]",
        help=(
            "Container type(s), comma-separated. "
            "Supported: base, linux, windows, android, web, xcode, osx, ios, "
            "all. The Apple chain (xcode -> osx -> ios) requires "
            "containers/files/Xcode_*.xip. Required unless "
            "--extract-sdks-only is given."
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
        "--extract-sdks-only",
        action="store_true",
        help=(
            "Run only the Apple SDK extraction step (docker run godot-xcode) "
            "without (re)building any image. Mutually exclusive with --push. "
            "Requires godot-xcode:<version> to be built locally already."
        ),
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

    # ------------------------------------------------------------------
    # release sub-command
    # ------------------------------------------------------------------
    release_p = sub.add_parser(
        "release",
        help="Build, package, and publish a GitHub Release for the matrix.",
        description=(
            "Drive the full build -> package -> publish flow: generate Mono "
            "glue once, build the configured matrix, package editor zips + "
            ".tpz + version.txt + SHA512-SUMS.txt, and publish them to a real "
            "GitHub Release on the configured tag."
        ),
    )
    release_p.add_argument(
        "--config",
        default="./config.toml",
        metavar="PATH",
        help="Path to the TOML configuration file.  (default: ./config.toml)",
    )
    release_p.add_argument(
        "--build",
        dest="do_build",
        action="store_true",
        default=True,
        help="Run the SCons builds. (default)",
    )
    release_p.add_argument(
        "--no-build",
        dest="do_build",
        action="store_false",
        help="Skip building; reuse existing out/.",
    )
    release_p.add_argument(
        "--package",
        dest="do_package",
        action="store_true",
        default=True,
        help="Run packaging. (default)",
    )
    release_p.add_argument(
        "--no-package",
        dest="do_package",
        action="store_false",
        help="Skip packaging; reuse existing release artifacts.",
    )
    release_p.add_argument(
        "--upload",
        dest="do_upload",
        action="store_true",
        default=None,
        help="Run `gh release` upload. (default: from [release].auto_upload)",
    )
    release_p.add_argument(
        "--no-upload",
        dest="do_upload",
        action="store_false",
        help="Stop after producing artifacts; do not publish.",
    )
    release_p.add_argument(
        "--tag",
        default=None,
        metavar="TAG",
        help="Target tag for the Release. (default: from [release].tag)",
    )
    release_p.add_argument(
        "--jobs",
        type=int,
        default=None,
        metavar="N",
        help=(
            "SCons -j parallelism. "
            "(default: from [build].build_jobs = nproc - 2, leaving 2 cores "
            "for the system)"
        ),
    )
    release_p.add_argument(
        "--godot-repo",
        default=None,
        metavar="SLUG",
        help="GitHub slug of the Godot source repo. (default: from config)",
    )
    release_p.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose (DEBUG-level) logging.",
    )
    release_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the build/package/gh commands without executing them.",
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
        logging.info(
            "Using Godot source from upstream/godot/ submodule (%s).", godot_repo
        )
        return _UPSTREAM_GODOT

    logging.warning(
        "upstream/godot/ submodule is not initialised. "
        "Run: git submodule update --init --recursive"
    )
    return _UPSTREAM_GODOT


# ---------------------------------------------------------------------------
# Build dispatch
# ---------------------------------------------------------------------------


def _resolve_matrix_for_platform(
    *,
    platform: str,
    config: dict,
    flavors: list[str],
    kinds: list[str],
    mono_variants: list[str],
    archs_override: list[str] | None,
) -> tuple[str, list[str]]:
    """Return the (platform_invariant_flags, derived SCons commands) for a platform.

    Raises :class:`SconsDerivationError` for an invalid flavor/kind/mono.
    """
    platform_cfg = get_platform_config(config, platform)
    platform_flags = platform_cfg["scons_flags"] if platform_cfg else ""
    archs = archs_override or get_platform_archs(config, platform)

    scons_cfg = get_scons_config(config)
    extra = scons_cfg["extra_flags"]
    if scons_cfg["use_lto"]:
        extra = (extra + " use_lto=yes").strip()

    invocations = derive_invocations(
        platform_flags=platform_flags,
        archs=archs,
        flavors=flavors,
        kinds=kinds,
        mono_variants=mono_variants,
        accesskit_sdk_path=scons_cfg["accesskit_sdk_path"],
        redirect_build_objects=scons_cfg["redirect_build_objects"],
        extra_flags=extra,
    )
    return platform_flags, [inv.to_command() for inv in invocations]


def _build_platform(
    platform: str,
    scons_commands: list[str],
    config: dict,
    source_dir: Path,
    output_dir: Path,
    dry_run: bool,
) -> int:
    """Dispatch the build for a single *platform* using derived SCons commands.

    Apple targets (macos/ios) always attempt to build. If the toolchain is not
    set up, the in-container build emits a clear actionable error and the run
    fails — there is no silent skip and no auto-detection.

    Returns
    -------
    int
        Exit code: 0 = success, 2 = unsupported/missing config,
        3 = Docker unavailable (no fallback), 4 = build process failed.
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
            'Add a [[platforms]] entry with name = "%s".',
            platform,
            platform,
        )
        return 2

    image = platform_cfg["image"]

    logger.info("Building platform=%s  image=%s", platform, image)
    for cmd in scons_commands:
        logger.info("  scons %s", cmd)

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
        for scons_flags in scons_commands:
            rc = run_build(
                image=image,
                scons_flags=scons_flags,
                source_dir=str(source_dir.resolve()),
                output_dir=str(output_dir.resolve()),
                dry_run=dry_run,
                env_setup=platform_cfg.get("env_setup", ""),
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

    for scons_flags in scons_commands:
        cmd = ["scons"] + scons_flags.split()
        logger.info("Running local SCons build: %s", " ".join(cmd))
        if dry_run:
            logger.info("[dry-run] Would run: %s", " ".join(cmd))
            continue
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

    # --extract-sdks-only is mutually exclusive with --push (extraction does
    # not produce a new image to push, and conflating the two would silently
    # do the wrong thing for operators).
    if args.extract_sdks_only and args.push:
        logger.error(
            "--extract-sdks-only and --push are mutually exclusive. "
            "Run them as separate invocations."
        )
        return 1

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

    containers_dir: Path = _SCRIPT_DIR / "containers"

    # --extract-sdks-only short-circuits the build flow: it just invokes the
    # extraction step against an already-built godot-xcode:<version> image.
    # Useful for operators who want to refresh the SDK tarballs without
    # touching the rest of the image set.
    if args.extract_sdks_only:
        return extract_apple_sdks(
            version=version,
            containers_dir=containers_dir,
            dry_run=args.dry_run,
            force=True,
        )

    # Parse type list. (Required when not in --extract-sdks-only mode.)
    if not args.type:
        logger.error(
            "No container types specified. Use --type base,linux,... "
            "(or pass --extract-sdks-only to only run the SDK extraction step)."
        )
        return 1
    types: list[str] = [t.strip().lower() for t in args.type.split(",") if t.strip()]
    if not types:
        logger.error("No container types specified. Use --type base,linux,...")
        return 1

    # Apple SDK version strings are config's single source of truth at
    # image-build time: they are passed as --build-arg to the xcode/osx/ios
    # Dockerfiles instead of being hand-edited in the ENV lines.
    build_cfg = get_build_config(config)

    return build_and_push(
        types=types,
        version=version,
        containers_dir=containers_dir,
        registry=registry,
        username=username,
        push=args.push,
        dry_run=args.dry_run,
        xcode_sdkv=build_cfg["xcode_sdkv"],
        apple_sdkv=build_cfg["apple_sdkv"],
    )


def _parse_platforms(raw: str) -> list[str]:
    """Expand the --platform CSV (handling 'all') into a concrete list."""
    items = [p.strip().lower() for p in raw.split(",") if p.strip()]
    if "all" in items:
        # Canonical build order: desktop Mono editor first (§6 risk mitigation).
        return ["linux", "windows", "macos", "android", "web", "ios"]
    return items


def _resolve_mono_variants(raw: str | None, default: list[str]) -> list[str]:
    """Resolve the --mono selector to a list of variants."""
    if raw is None:
        return list(default)
    if raw == "both":
        return ["on", "off"]
    return [raw]


def _resolve_csv(raw: str | None, default: list[str]) -> list[str]:
    if raw is None:
        return list(default)
    return [v.strip() for v in raw.split(",") if v.strip()]


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
    platforms = _parse_platforms(args.platform)
    if not platforms:
        logger.error("No platforms specified. Use --platform linux,windows,...")
        return 1

    build_cfg = get_build_config(config)
    flavors = _resolve_csv(args.flavor, build_cfg["flavors"])
    kinds = _resolve_csv(args.kind, build_cfg["kinds"])
    mono_variants = _resolve_mono_variants(args.mono, build_cfg["mono"])
    archs_override = (
        [a.strip() for a in args.arch.split(",") if a.strip()] if args.arch else None
    )

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

        # Deprecated escape hatch: --target bypasses flavor/kind derivation.
        if args.target is not None:
            logger.warning(
                "--target is deprecated; passing '%s' to SCons verbatim "
                "and bypassing flavor/kind/mono derivation.",
                args.target,
            )
            platform_cfg = get_platform_config(config, platform)
            platform_flags = platform_cfg["scons_flags"] if platform_cfg else ""
            scons_commands = [
                " ".join(f for f in [platform_flags, f"target={args.target}"] if f)
            ]
        else:
            try:
                _, scons_commands = _resolve_matrix_for_platform(
                    platform=platform,
                    config=config,
                    flavors=flavors,
                    kinds=kinds,
                    mono_variants=mono_variants,
                    archs_override=archs_override,
                )
            except SconsDerivationError as exc:
                logger.error("%s", exc)
                return 1

        try:
            rc = _build_platform(
                platform=platform,
                scons_commands=scons_commands,
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


def cmd_release(args: argparse.Namespace) -> int:
    """Handle the ``release`` sub-command: build -> package -> publish."""
    _configure_logging(args.verbose)
    logger = logging.getLogger(__name__)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        logger.error("%s", exc)
        return 1

    build_cfg = get_build_config(config)
    release_cfg = get_release_config(config)

    num_cores = args.jobs if args.jobs is not None else build_cfg["build_jobs"]
    tag = args.tag if args.tag is not None else release_cfg["tag"]
    repo = release_cfg["repo"]
    do_upload = (
        args.do_upload if args.do_upload is not None else release_cfg["auto_upload"]
    )
    godot_repo = args.godot_repo or config.get("godot_repo", _DEFAULT_GODOT_REPO)
    git_branch = config.get("git_branch", DEFAULT_GIT_BRANCH)
    # The host orchestrator resolves the container image tags BEFORE it
    # extracts the engine version, so we hand it the Godot version up front (as
    # container_version) so the tags match config.toml / `containers --push`.
    godot_version = config["godot_version"]

    # Build type from the configured mono matrix.
    mono = build_cfg["mono"]
    if "on" in mono and "off" in mono:
        build_type = "all"
    elif "on" in mono:
        build_type = "mono"
    else:
        build_type = "classical"

    # --- Build (host orchestrator: deps + tarball + per-platform docker passes) ---
    if args.do_build:
        logger.info(
            "--- Building matrix (build_type=%s, -j%d) ---", build_type, num_cores
        )
        rc = dispatch_build(
            build_dir=_BUILD_AND_TEMPLATES_DIR,
            build_type=build_type,
            num_cores=num_cores,
            git_branch=git_branch,
            godot_repo=godot_repo,
            registry=config["registry"],
            username=config["username"],
            container_version=godot_version,
            dry_run=args.dry_run,
        )
        if rc != 0:
            logger.error("Build step exited with code %d.", rc)
            return 4
    else:
        logger.info("Skipping build step (--no-build).")

    # Status suffix derives from the configured branch tail (e.g. 4.7.dev1 -> dev1).
    status = git_branch.split(".")[-1] if "." in git_branch else git_branch
    binaries_version = f"{godot_version}-{status}"

    # --- Package (scripts.packager: editor zips + .tpz + SHA512-SUMS.txt) ---
    if args.do_package:
        logger.info("--- Packaging artifacts ---")
        rc = package_release(
            build_dir=_BUILD_AND_TEMPLATES_DIR,
            godot_version=godot_version,
            godot_version_status=status,
            dry_run=args.dry_run,
        )
        if rc != 0:
            logger.error("Package step exited with code %d.", rc)
            return 4
    else:
        logger.info("Skipping package step (--no-package).")

    # --- Publish (gh release) ---
    if not do_upload:
        logger.info("Skipping publish step (--no-upload / auto_upload=false).")
        return 0

    release_dir = _BUILD_AND_TEMPLATES_DIR / "releases" / binaries_version
    assets = collect_release_assets(release_dir)
    if args.dry_run and not assets:
        # In dry-run the build/package did not actually create files; show the
        # intended publish command against the expected release dir.
        logger.info(
            "[dry-run] Release dir would be: %s (assets collected at publish time).",
            release_dir,
        )
        # Representative real asset path (editor zip) so the printed `gh release`
        # command mirrors a true publish rather than a literal placeholder.
        assets = [release_dir / f"Godot_v{binaries_version}_linux.x86_64.zip"]

    try:
        publish_release(
            tag=tag,
            repo=repo,
            assets=assets,
            prerelease=release_cfg["prerelease"],
            draft=release_cfg["draft"],
            dry_run=args.dry_run,
        )
    except PublishError as exc:
        logger.error("Release publish failed: %s", exc)
        return 5

    logger.info("Release '%s' published to %s.", tag, repo)
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

    if args.command == "release":
        return cmd_release(args)

    # Should never reach here because sub.required = True.
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
