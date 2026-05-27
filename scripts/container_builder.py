"""Container builder: build and push Godot Docker images.

All Docker invocations for the ``containers`` sub-command go through this
module so the rest of the codebase stays free of subprocess boilerplate.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

SUPPORTED_TYPES: set[str] = {"base", "linux", "windows", "android", "web"}

# Build order — base must always come first.
_BUILD_ORDER: list[str] = ["base", "linux", "windows", "android", "web"]

# Map container type → local image name (without tag).
_IMAGE_NAME: dict[str, str] = {
    "base": "godot-fedora",
    "linux": "godot-linux",
    "windows": "godot-windows",
    "android": "godot-android",
    "web": "godot-web",
}

# Map container type → Dockerfile name (relative to containers_dir).
_DOCKERFILE: dict[str, str] = {
    "base": "Dockerfile.base",
    "linux": "Dockerfile.linux",
    "windows": "Dockerfile.windows",
    "android": "Dockerfile.android",
    "web": "Dockerfile.web",
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ContainerBuildError(Exception):
    """Raised when a ``docker build`` or ``docker push`` subprocess fails."""


class UnsupportedTypeError(Exception):
    """Raised when an unsupported container type is requested."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_image(
    container_type: str,
    version: str,
    containers_dir: Path,
    registry: str,  # noqa: ARG001 — kept for API symmetry / future use
    username: str,  # noqa: ARG001 — kept for API symmetry / future use
    dry_run: bool = False,
) -> None:
    """Build a single container image via ``docker build``.

    Parameters
    ----------
    container_type:
        One of :data:`SUPPORTED_TYPES`.
    version:
        Image version tag, e.g. ``"4.7"``.
    containers_dir:
        Path to the directory that contains the Dockerfiles.
    registry:
        Registry hostname (reserved for future use; build is always local).
    username:
        Registry username (reserved for future use; build is always local).
    dry_run:
        When ``True``, log the command but do not execute it.

    Raises
    ------
    UnsupportedTypeError
        If *container_type* is not in :data:`SUPPORTED_TYPES`.
    ContainerBuildError
        If the ``docker build`` process exits non-zero.
    """
    if container_type not in SUPPORTED_TYPES:
        raise UnsupportedTypeError(
            f"Unsupported container type '{container_type}'. "
            f"Supported types: {', '.join(sorted(SUPPORTED_TYPES))}."
        )

    image_name = _IMAGE_NAME[container_type]
    dockerfile = _DOCKERFILE[container_type]
    full_tag = f"{image_name}:{version}"
    dockerfile_path = str(containers_dir / dockerfile)
    context_path = str(containers_dir)

    if container_type == "base":
        cmd = [
            "docker", "build",
            "-t", full_tag,
            "-f", dockerfile_path,
            context_path,
        ]
    else:
        cmd = [
            "docker", "build",
            "--build-arg", f"IMAGE_VERSION={version}",
            "-t", full_tag,
            "-f", dockerfile_path,
            context_path,
        ]

    if dry_run:
        logger.info("[dry-run] Would run: %s", " ".join(cmd))
        return

    logger.info("Building container image: %s", full_tag)
    logger.debug("Full command: %s", " ".join(cmd))
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        raise ContainerBuildError(
            f"docker build for '{container_type}' failed with exit code {exc.returncode}."
        ) from exc


def push_image(
    container_type: str,
    version: str,
    registry: str,
    username: str,
    dry_run: bool = False,
) -> None:
    """Tag and push a locally-built image to *registry*.

    Reads the ``GHCR_PAT`` environment variable for authentication.  If it is
    not set, logs a warning and returns without raising (graceful skip).

    Parameters
    ----------
    container_type:
        One of :data:`SUPPORTED_TYPES`.
    version:
        Image version tag, e.g. ``"4.7"``.
    registry:
        Registry hostname, e.g. ``"ghcr.io"``.
    username:
        Registry username, e.g. ``"nongvantinh"``.
    dry_run:
        When ``True``, log the commands but do not execute them.

    Raises
    ------
    UnsupportedTypeError
        If *container_type* is not in :data:`SUPPORTED_TYPES`.
    ContainerBuildError
        If any Docker subprocess exits non-zero.
    """
    if container_type not in SUPPORTED_TYPES:
        raise UnsupportedTypeError(
            f"Unsupported container type '{container_type}'. "
            f"Supported types: {', '.join(sorted(SUPPORTED_TYPES))}."
        )

    pat = os.environ.get("GHCR_PAT")
    if not pat:
        logger.warning(
            "GHCR_PAT environment variable is not set — skipping push for '%s'.\n"
            "Export your GitHub Personal Access Token before pushing:\n"
            "  export GHCR_PAT=<your-token>",
            container_type,
        )
        return

    local_name = _IMAGE_NAME[container_type]
    local_tag = f"{local_name}:{version}"
    remote_tag = f"{registry}/{username}/{local_name}:{version}"

    # --- docker login ---
    login_cmd = [
        "docker", "login", registry,
        "--username", username,
        "--password-stdin",
    ]
    if dry_run:
        logger.info("[dry-run] Would run: echo $GHCR_PAT | %s", " ".join(login_cmd))
    else:
        logger.info("Logging in to %s as %s", registry, username)
        try:
            subprocess.run(login_cmd, input=pat.encode(), check=True)
        except subprocess.CalledProcessError as exc:
            raise ContainerBuildError(
                f"docker login to '{registry}' failed with exit code {exc.returncode}."
            ) from exc

    # --- docker tag ---
    tag_cmd = ["docker", "tag", local_tag, remote_tag]
    if dry_run:
        logger.info("[dry-run] Would run: %s", " ".join(tag_cmd))
    else:
        logger.info("Tagging %s → %s", local_tag, remote_tag)
        try:
            subprocess.run(tag_cmd, check=True)
        except subprocess.CalledProcessError as exc:
            raise ContainerBuildError(
                f"docker tag failed with exit code {exc.returncode}."
            ) from exc

    # --- docker push ---
    push_cmd = ["docker", "push", remote_tag]
    if dry_run:
        logger.info("[dry-run] Would run: %s", " ".join(push_cmd))
        return

    logger.info("Pushing %s", remote_tag)
    try:
        subprocess.run(push_cmd, check=True)
    except subprocess.CalledProcessError as exc:
        raise ContainerBuildError(
            f"docker push for '{remote_tag}' failed with exit code {exc.returncode}."
        ) from exc


def is_image_built(container_type: str, version: str) -> bool:
    """Return ``True`` if a local Docker image for *container_type* exists.

    Parameters
    ----------
    container_type:
        One of :data:`SUPPORTED_TYPES`.
    version:
        Image version tag, e.g. ``"4.7"``.
    """
    if container_type not in SUPPORTED_TYPES:
        return False

    image_name = _IMAGE_NAME[container_type]
    expected = f"{image_name}:{version}"

    result = subprocess.run(
        ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
        capture_output=True,
        text=True,
    )
    return expected in result.stdout.splitlines()


def build_and_push(
    types: list[str],
    version: str,
    containers_dir: Path,
    registry: str,
    username: str,
    push: bool,
    dry_run: bool,
) -> int:
    """Orchestrate building (and optionally pushing) a set of container types.

    - Resolves ``"all"`` to every type in :data:`SUPPORTED_TYPES`.
    - Always builds ``base`` first, even when not explicitly requested.
    - Builds types in canonical order: base → linux → windows → android → web.
    - Skips building if the image already exists locally (unless ``dry_run``).
    - Pushes each image when *push* is ``True``.

    Parameters
    ----------
    types:
        List of container type strings (may include ``"all"``).
    version:
        Image version tag, e.g. ``"4.7"``.
    containers_dir:
        Path to the directory containing the Dockerfiles.
    registry:
        Registry hostname for push operations.
    username:
        Registry username for push operations.
    push:
        When ``True``, push each image to the registry after building.
    dry_run:
        When ``True``, log commands without executing them.

    Returns
    -------
    int
        Exit code: 0 = success, 2 = unsupported type, 3 = Docker unavailable,
        4 = build/push subprocess failed.
    """
    # Validate Docker availability.
    if shutil.which("docker") is None:
        logger.error(
            "docker binary not found on PATH.\n"
            "Install Docker: https://docs.docker.com/get-docker/"
        )
        return 3

    # Resolve "all".
    resolved: set[str] = set()
    for t in types:
        t = t.strip().lower()
        if t == "all":
            resolved |= SUPPORTED_TYPES
        elif t in SUPPORTED_TYPES:
            resolved.add(t)
        else:
            logger.error(
                "Unsupported container type '%s'. Supported types: %s.",
                t,
                ", ".join(sorted(SUPPORTED_TYPES)),
            )
            return 2

    # Ensure base is always included when other types are requested.
    if resolved - {"base"}:
        resolved.add("base")

    # Build in canonical order.
    ordered: list[str] = [t for t in _BUILD_ORDER if t in resolved]

    for container_type in ordered:
        if not dry_run and is_image_built(container_type, version):
            logger.info(
                "Image %s:%s already exists locally — skipping build.",
                _IMAGE_NAME[container_type],
                version,
            )
        else:
            logger.info("--- Building container: %s ---", container_type)
            try:
                build_image(
                    container_type=container_type,
                    version=version,
                    containers_dir=containers_dir,
                    registry=registry,
                    username=username,
                    dry_run=dry_run,
                )
            except UnsupportedTypeError as exc:
                logger.error("%s", exc)
                return 2
            except ContainerBuildError as exc:
                logger.error("%s", exc)
                return 4

        if push:
            logger.info("--- Pushing container: %s ---", container_type)
            try:
                push_image(
                    container_type=container_type,
                    version=version,
                    registry=registry,
                    username=username,
                    dry_run=dry_run,
                )
            except UnsupportedTypeError as exc:
                logger.error("%s", exc)
                return 2
            except ContainerBuildError as exc:
                logger.error("%s", exc)
                return 4

    return 0
