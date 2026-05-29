"""Release orchestration: drive the host orchestrator, run the Python
packager, and publish.

This module owns:

  - driving :func:`scripts.host_orchestrator.run_build` via :func:`dispatch_build`
    to build the matrix (Mono glue + per-platform passes),
  - calling :func:`scripts.packager.package_release` via :func:`package_release`
    to stage editor zips, .tpz bundles and SHA512-SUMS.txt files,
  - publishing a real GitHub Release via ``gh``.

All subprocess and ``gh`` calls are funnelled through small, individually
patchable functions so tests can exercise the wiring without Docker, the
network, or a real ``gh``.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from scripts import host_orchestrator, packager

logger = logging.getLogger(__name__)


class PublishError(Exception):
    """Raised when the GitHub Release publish step fails (maps to exit code 5)."""


# ---------------------------------------------------------------------------
# Build + packaging (all-Python pipeline)
# ---------------------------------------------------------------------------


def dispatch_build(
    *,
    build_dir: Path,
    build_type: str,
    num_cores: int,
    git_branch: str,
    godot_repo: str,
    registry: str,
    username: str,
    container_version: str,
    dry_run: bool = False,
) -> int:
    """Drive the host orchestrator to build the matrix in-process.

    Delegates to :func:`scripts.host_orchestrator.run_build` — generate the
    Mono glue once, pull images, run per-platform docker passes with the
    resumability gate. Apple targets (macos/ios) are always attempted; the
    in-container build emits a clear error if its toolchain is not set up.

    *container_version* is the Godot version (e.g. ``"4.7"``) and resolves
    image refs as ``{registry}/{username}/godot-<plat>:{container_version}``
    — matching what ``containers --push`` produces and what ``config.toml``
    declares. It must be threaded here because the build resolves images
    *before* it extracts the engine version, so we cannot derive the tag
    from the source tree.

    The Apple SDK *version* strings (``XCODE_SDKV`` / ``APPLE_SDKV``) are NOT
    threaded here: they are config's single source of truth at image-build
    time, baked into the Apple images via ``container_builder``
    ``--build-arg``. The host orchestrator never consumes them at run time.

    Returns the exit code (0 = success).
    """
    upstream_godot_dir = (build_dir.parent / "upstream" / "godot").resolve()
    return host_orchestrator.run_build(
        basedir=build_dir,
        registry=registry,
        username=username,
        container_version=container_version,
        git_branch=git_branch,
        godot_repo=godot_repo,
        upstream_godot_dir=upstream_godot_dir,
        build_type=build_type,
        num_cores=num_cores,
        dry_run=dry_run,
    )


def package_release(
    *,
    build_dir: Path,
    godot_version: str,
    godot_version_status: str,
    upstream_godot_dir: Path | None = None,
    dry_run: bool = False,
) -> int:
    """Run the host-side packaging step.

    Calls :func:`scripts.packager.package_release` in-process: emit editor
    zips, classical + Mono ``.tpz`` export-template bundles (each with
    ``version.txt``), and ``SHA512-SUMS.txt``. Publishing is owned by
    :func:`publish_release`.

    Parameters
    ----------
    build_dir
        ``build-godot-and-templates/`` — the dir holding ``out/`` /
        ``releases/`` / ``deps/`` etc.
    godot_version, godot_version_status
        Engine version + pre-release status, e.g. ``"4.7"`` / ``"dev1"``.
    upstream_godot_dir
        Optional override for the path to the checked-out Godot source. When
        ``None`` (the default), the packager picks
        ``<build_dir>.parent/upstream/godot`` — the path used by the rest of
        ``build-godot.py``.
    dry_run
        Log actions without writing anything.

    Returns ``0`` on success; non-zero on a hard packaging error.
    """
    if dry_run:
        logger.info(
            "[dry-run] Would run packager.package_release(basedir=%s, "
            "godot_version=%s, godot_version_status=%s, upstream_godot_dir=%s).",
            build_dir,
            godot_version,
            godot_version_status,
            upstream_godot_dir,
        )
        return 0
    return packager.package_release(
        basedir=build_dir,
        godot_version=godot_version,
        godot_version_status=godot_version_status,
        upstream_godot_dir=upstream_godot_dir,
        dry_run=False,
    )


def collect_release_assets(release_dir: Path) -> list[Path]:
    """Return every publishable asset under *release_dir* (recursively).

    Includes editor zips, ``.tpz`` bundles, and ``SHA512-SUMS.txt`` from both
    the classical dir and the ``mono/`` subdir.
    """
    if not release_dir.is_dir():
        return []
    return sorted(p for p in release_dir.rglob("*") if p.is_file())


# ---------------------------------------------------------------------------
# Publishing (gh release)
# ---------------------------------------------------------------------------


def _gh_available() -> bool:
    return shutil.which("gh") is not None


def _release_exists(tag: str, repo: str, dry_run: bool = False) -> bool:
    """Return ``True`` if a GitHub Release already exists for *tag*."""
    if dry_run:
        # In dry-run we cannot query; assume it must be created (upload would
        # otherwise need --clobber). The dry-run command print below shows both.
        return False
    result = subprocess.run(
        ["gh", "release", "view", tag, "--repo", repo],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def build_publish_command(
    *,
    tag: str,
    repo: str,
    assets: list[str],
    prerelease: bool,
    draft: bool,
    release_exists: bool,
) -> list[str]:
    """Construct the ``gh release create|upload`` command (no execution).

    Pure helper so tests can assert the exact command without invoking ``gh``.
    """
    if release_exists:
        cmd = ["gh", "release", "upload", tag, *assets, "--repo", repo, "--clobber"]
        return cmd
    cmd = ["gh", "release", "create", tag, *assets, "--repo", repo]
    if prerelease:
        cmd.append("--prerelease")
    if draft:
        cmd.append("--draft")
    # Reuse the existing tag without re-tagging or generating extra notes.
    # NOTE: ``--clobber`` is an ``upload``-only flag; ``gh release create`` rejects
    # it ("unknown flag"). It belongs only on the ``upload`` branch above.
    cmd.extend(["--title", tag, "--notes", f"Custom Godot build {tag}."])
    return cmd


def publish_release(
    *,
    tag: str,
    repo: str,
    assets: list[Path],
    prerelease: bool,
    draft: bool,
    dry_run: bool = False,
) -> None:
    """Publish *assets* to the GitHub Release on *tag*.

    Uses ``gh release create`` when no Release exists for the tag, else
    ``gh release upload ... --clobber`` (idempotent re-upload).

    Raises
    ------
    PublishError
        If ``gh`` is unavailable, there are no assets, or the publish exits
        non-zero. The caller maps this to exit code 5.
    """
    if not dry_run and not _gh_available():
        raise PublishError(
            "gh CLI not found on PATH. Install GitHub CLI and run "
            "`gh auth login` before publishing a Release."
        )
    if not assets:
        raise PublishError(
            f"No release assets found to publish for tag '{tag}'. "
            "Run the build + package steps first."
        )

    exists = _release_exists(tag, repo, dry_run=dry_run)
    cmd = build_publish_command(
        tag=tag,
        repo=repo,
        assets=[str(a) for a in assets],
        prerelease=prerelease,
        draft=draft,
        release_exists=exists,
    )

    if dry_run:
        logger.info("[dry-run] Would run: %s", " ".join(cmd))
        return

    logger.info("Publishing %d asset(s) to Release '%s' on %s", len(assets), tag, repo)
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        raise PublishError(
            f"`gh release` failed with exit code {exc.returncode} for tag '{tag}'."
        ) from exc
