"""Docker helper: pull, login, and run build containers.

All Docker invocations go through this module so the rest of the codebase
stays free of subprocess boilerplate.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

from scripts.config import ConfigError

logger = logging.getLogger(__name__)


class BuildError(Exception):
    """Raised when a Docker build, pull, or login operation fails."""


class DockerUnavailableError(Exception):
    """Raised when Docker daemon is not reachable."""


def ensure_docker() -> None:
    """Verify that Docker is installed and the daemon is running.

    Raises :class:`DockerUnavailableError` if ``docker info`` exits non-zero
    or if the ``docker`` binary is not on PATH.
    """
    if shutil.which("docker") is None:
        raise DockerUnavailableError(
            "docker binary not found on PATH.\n"
            "Install Docker: https://docs.docker.com/get-docker/"
        )
    result = subprocess.run(
        ["docker", "info"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise DockerUnavailableError(
            "Docker daemon is not running (docker info exited with "
            f"{result.returncode}).\n"
            "Start the Docker daemon and try again."
        )
    logger.debug("Docker daemon is available.")


def pull_image(image: str, dry_run: bool = False) -> None:
    """Pull *image* from its registry.

    Parameters
    ----------
    image:
        Full image reference, e.g. ``ghcr.io/nongvantinh/linux-editor:4.3``.
    dry_run:
        When ``True``, print the command but do not execute it.
    """
    cmd = ["docker", "pull", image]
    if dry_run:
        logger.info("[dry-run] Would run: %s", " ".join(cmd))
        return
    logger.info("Pulling image: %s", image)
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        raise BuildError(
            f"Failed to pull image '{image}': docker exited {exc.returncode}"
        ) from exc


def run_build(
    image: str,
    scons_flags: str,
    source_dir: str,
    output_dir: str,
    dry_run: bool = False,
) -> int:
    """Run a Godot build inside *image*.

    Mounts *source_dir* as ``/root/godot`` (read-write) and *output_dir* as
    ``/root/out`` inside the container.

    Parameters
    ----------
    image:
        Container image to use.
    scons_flags:
        SCons flags string, e.g. ``"platform=linuxbsd target=editor"``.
    source_dir:
        Absolute path to the Godot source directory on the host.
    output_dir:
        Absolute path where build artefacts should be written.
    dry_run:
        When ``True``, print the command and return 0 without running it.

    Returns
    -------
    int
        Exit code of the Docker subprocess (0 = success).
    """
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{source_dir}:/root/godot",
        "-v", f"{output_dir}:/root/out",
        image,
        "scons",
    ] + scons_flags.split()

    if dry_run:
        logger.info("[dry-run] Would run: %s", " ".join(cmd))
        return 0

    logger.info("Running build in container %s", image)
    logger.debug("Full command: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        raise BuildError(
            f"Build container '{image}' exited unexpectedly: {exc.returncode}"
        ) from exc
    return result.returncode


def login(registry: str, username: str, dry_run: bool = False) -> None:
    """Log in to *registry* using the ``GHCR_PAT`` environment variable.

    Parameters
    ----------
    registry:
        Registry hostname, e.g. ``ghcr.io``.
    username:
        Registry username.
    dry_run:
        When ``True``, print the command but do not execute it.

    Raises
    ------
    ConfigError
        If ``GHCR_PAT`` is not set in the environment.
    """
    pat = os.environ.get("GHCR_PAT")
    if not pat:
        raise ConfigError(
            "GHCR_PAT environment variable is not set.\n"
            "Export your GitHub Personal Access Token before running:\n"
            "  export GHCR_PAT=<your-token>"
        )
    cmd = [
        "docker", "login", registry,
        "--username", username,
        "--password-stdin",
    ]
    if dry_run:
        logger.info("[dry-run] Would run: echo $GHCR_PAT | %s", " ".join(cmd))
        return
    logger.info("Logging in to %s as %s", registry, username)
    try:
        subprocess.run(cmd, input=pat.encode(), check=True)
    except subprocess.CalledProcessError as exc:
        raise BuildError(
            f"docker login to '{registry}' failed: docker exited {exc.returncode}"
        ) from exc
