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

SUPPORTED_TYPES: set[str] = {
    "base",
    "linux",
    "windows",
    "android",
    "web",
    "xcode",
    "osx",
    "ios",
}

# Build order — base must always come first; the Apple chain is ordered
# xcode -> osx -> ios because osx consumes the SDK tarballs produced by xcode
# and ios is built FROM godot-osx (Directive 2).
_BUILD_ORDER: list[str] = [
    "base",
    "linux",
    "windows",
    "android",
    "web",
    "xcode",
    "osx",
    "ios",
]

# Map container type → local image name (without tag).
_IMAGE_NAME: dict[str, str] = {
    "base": "godot-fedora",
    "linux": "godot-linux",
    "windows": "godot-windows",
    "android": "godot-android",
    "web": "godot-web",
    "xcode": "godot-xcode",
    "osx": "godot-osx",
    "ios": "godot-ios",
}

# Map container type → Dockerfile name (relative to containers_dir).
_DOCKERFILE: dict[str, str] = {
    "base": "Dockerfile.base",
    "linux": "Dockerfile.linux",
    "windows": "Dockerfile.windows",
    "android": "Dockerfile.android",
    "web": "Dockerfile.web",
    "xcode": "Dockerfile.xcode",
    "osx": "Dockerfile.osx",
    "ios": "Dockerfile.ios",
}

# Apple image types whose Dockerfiles consume the XCODE_SDKV / APPLE_SDKV
# build-args. Config is the single source of truth for the SDK version strings
# at image-build time; a bare `docker build` still works via the Dockerfile
# ENV defaults.
_APPLE_IMAGE_TYPES: set[str] = {"xcode", "osx", "ios"}

# Image types that consume the Apple SDK tarballs produced by running godot-xcode
# (osx links them via Dockerfile symlinks; ios builds FROM godot-osx). When the
# operator requests any of these, the SDK-extraction step must run after xcode
# is built and before these images are built.
_APPLE_SDK_CONSUMER_TYPES: set[str] = {"osx", "ios"}

# Glob patterns under containers_dir/files for SDK tarballs produced by
# extract_xcode_sdks.sh. If any of these match, extraction is considered already
# done and we skip the docker run (matches the "already built" idempotency
# pattern). MacOSX*.sdk.tar.xz is the canonical proof; Xcode-Developer*.tar.xz
# is the second tarball the script emits when EXTRACT_XCODE=1 (the default for
# image-context runs). iPhoneOS / iPhoneSimulator are listed for completeness
# because the helper script may grow those branches; today they ship inside the
# Xcode-Developer tree.
_SDK_TARBALL_GLOBS: tuple[str, ...] = (
    "MacOSX*.sdk.tar.xz",
    "Xcode-Developer*.tar.xz",
    "iPhoneOS*.sdk.tar.xz",
    "iPhoneSimulator*.sdk.tar.xz",
)


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
    xcode_sdkv: str | None = None,
    apple_sdkv: str | None = None,
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
    xcode_sdkv:
        Xcode SDK version string from ``[build].xcode_sdkv``. Passed as
        ``--build-arg XCODE_SDKV`` for the Apple image types (xcode/osx/ios) so
        config is the single source of truth at image-build time.
    apple_sdkv:
        Apple/macOS SDK version string from ``[build].apple_sdkv``. Passed as
        ``--build-arg APPLE_SDKV`` for the Apple image types.

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
            "docker",
            "build",
            "-t",
            full_tag,
            "-f",
            dockerfile_path,
            context_path,
        ]
    else:
        cmd = [
            "docker",
            "build",
            "--build-arg",
            f"IMAGE_VERSION={version}",
        ]
        # Apple images: bake the SDK version strings from config at build
        # time. The Dockerfile ENV defaults keep a bare `docker build` working
        # when these build-args are omitted.
        if container_type in _APPLE_IMAGE_TYPES:
            if xcode_sdkv:
                cmd += ["--build-arg", f"XCODE_SDKV={xcode_sdkv}"]
            if apple_sdkv:
                cmd += ["--build-arg", f"APPLE_SDKV={apple_sdkv}"]
        cmd += [
            "-t",
            full_tag,
            "-f",
            dockerfile_path,
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
        "docker",
        "login",
        registry,
        "--username",
        username,
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


def _existing_sdk_tarballs(files_dir: Path) -> list[Path]:
    """Return any Apple SDK tarballs already present in *files_dir*.

    Used as the idempotency probe for :func:`extract_apple_sdks` — if any of
    the expected tarballs exist we skip the docker run (mirrors the
    ``is_image_built`` skip pattern used elsewhere in this module).
    """
    if not files_dir.is_dir():
        return []
    found: list[Path] = []
    for pattern in _SDK_TARBALL_GLOBS:
        found.extend(files_dir.glob(pattern))
    return found


def extract_apple_sdks(
    version: str,
    containers_dir: Path,
    dry_run: bool,
    force: bool = False,
) -> int:
    """Run ``godot-xcode:<version>`` to extract Apple SDK tarballs.

    The ``godot-xcode`` image's CMD is ``/root/files/extract_xcode_sdks.sh``,
    which unpacks the operator-provided ``Xcode_${XCODE_SDKV}.xip`` and writes
    ``MacOSX${APPLE_SDKV}.sdk.tar.xz`` + ``Xcode-Developer${XCODE_SDKV}.tar.xz``
    into ``/root/files`` (bind-mounted to ``containers/files`` on the host).
    The ``osx``/``ios`` Dockerfiles then consume those tarballs at image-build
    time, so this step MUST run between ``build xcode`` and ``build osx``.

    Idempotency mirrors :func:`is_image_built`: if any expected tarball is
    already present under ``<containers_dir>/files/`` the extraction is
    skipped, unless *force* is ``True``.

    The image's ``XCODE_SDKV`` / ``APPLE_SDKV`` ENV defaults (set by the
    ``--build-arg`` values at image-build time) are already correct, so we do NOT
    re-thread them through ``docker run -e`` — the script reads them from the
    image environment. (If a future bump moves them outside the image we can
    surface them through here, but today doing so would only mask a stale
    image build.)

    Returns
    -------
    int
        Exit code: ``0`` on success (or skipped because tarballs already
        exist), ``4`` on docker-run failure or when prerequisites are absent.
    """
    files_dir = containers_dir / "files"

    # Idempotency: tarballs already extracted -> nothing to do (unless forced).
    existing = _existing_sdk_tarballs(files_dir)
    if existing and not force:
        names = ", ".join(sorted(p.name for p in existing))
        logger.info(
            "Apple SDK tarballs already present in %s (%s) — skipping extraction.",
            files_dir,
            names,
        )
        return 0

    # Hard-error: at this point the caller has chosen to build osx/ios, so the
    # operator MUST have provided either the xip or pre-extracted tarballs.
    # (existing is empty here because either there were none, or force=True;
    #  with force=True we still need the xip to actually run extraction.)
    has_xip = files_dir.is_dir() and any(files_dir.glob("Xcode_*.xip"))
    if not has_xip and not existing:
        logger.error(
            "godot-xcode requires containers/files/Xcode_*.xip OR pre-extracted "
            "SDK tarballs (MacOSX*.sdk.tar.xz / Xcode-Developer*.tar.xz). "
            "Neither was found under %s — cannot extract Apple SDKs.",
            files_dir,
        )
        return 4

    # Idempotency edge case: the operator may invoke this without first
    # building godot-xcode (e.g. `--extract-sdks-only` on a fresh checkout).
    # Don't try to pull from a registry; the image may not be pushed yet.
    if not dry_run and not is_image_built("xcode", version):
        logger.error(
            "godot-xcode:%s image not found locally; "
            "run `containers --type xcode` first.",
            version,
        )
        return 4

    image_tag = f"{_IMAGE_NAME['xcode']}:{version}"
    volume_mount = f"{files_dir}:/root/files"
    cmd = [
        "docker",
        "run",
        "--rm",
        "-v",
        volume_mount,
        image_tag,
    ]

    if dry_run:
        logger.info("[dry-run] Would run: %s", " ".join(cmd))
        return 0

    logger.info(
        "--- Extracting Apple SDK tarballs via %s -> %s ---", image_tag, files_dir
    )
    logger.debug("Full command: %s", " ".join(cmd))
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        logger.error(
            "docker run for '%s' failed with exit code %d.",
            image_tag,
            exc.returncode,
        )
        return 4

    return 0


def build_and_push(
    types: list[str],
    version: str,
    containers_dir: Path,
    registry: str,
    username: str,
    push: bool,
    dry_run: bool,
    xcode_sdkv: str | None = None,
    apple_sdkv: str | None = None,
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
    xcode_sdkv:
        Xcode SDK version string from ``[build].xcode_sdkv``, forwarded to
        :func:`build_image` as ``--build-arg XCODE_SDKV`` for Apple images.
    apple_sdkv:
        Apple/macOS SDK version string from ``[build].apple_sdkv``, forwarded as
        ``--build-arg APPLE_SDKV`` for Apple images.

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

    # The SDK-extraction step must run between `build xcode` and `build osx`
    # (osx/ios consume the tarballs produced by running godot-xcode). We trigger
    # it lazily — the first time the loop encounters a consumer type — so
    # operators who request only `xcode` (or only non-Apple types) never pay
    # for it.
    needs_apple_extraction: bool = bool(resolved & _APPLE_SDK_CONSUMER_TYPES)
    apple_extraction_done: bool = False

    for container_type in ordered:
        # Just-in-time SDK extraction (Apple chain orchestration gap):
        # before the first osx/ios build, run godot-xcode to write the SDK
        # tarballs into containers/files/. xcode itself was already built
        # earlier in this same loop iteration order.
        if (
            needs_apple_extraction
            and not apple_extraction_done
            and container_type in _APPLE_SDK_CONSUMER_TYPES
        ):
            rc = extract_apple_sdks(
                version=version,
                containers_dir=containers_dir,
                dry_run=dry_run,
            )
            if rc != 0:
                return rc
            apple_extraction_done = True

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
                    xcode_sdkv=xcode_sdkv,
                    apple_sdkv=apple_sdkv,
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
