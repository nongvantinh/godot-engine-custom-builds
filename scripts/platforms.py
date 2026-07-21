"""Single source of truth for the platform matrix.

Every platform-specific fact the orchestrator needs — the container image
basename, whether Docker is mandatory, whether the in-container build honours
arch scoping, which host dependency dirs to mount, and any extra container env
— lives here as data. Consumers (``build-godot.py``, ``host_orchestrator``)
derive their behaviour from this registry instead of repeating hardcoded
platform lists, so adding or adjusting a platform is a one-line data edit
rather than a hunt across modules.

The tuple order is the canonical release build order: Linux first (its image
generates the Mono glue and it is the fastest desktop editor to validate),
then the remaining platforms. Each platform build is otherwise independent, so
the order only affects when a given platform's artifacts appear.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Platform:
    """Immutable description of one build platform.

    name:
        Platform identifier passed to ``--platform`` and used as the
        ``out/<name>`` directory (e.g. ``"macos"``).
    image_basename:
        Container image name stem under ``{registry}/{username}/`` — usually
        ``godot-<name>`` but not always (macOS uses ``godot-osx``).
    docker_required:
        When ``False`` the platform can fall back to a local SCons build if
        Docker is unavailable (Linux only).
    arch_scoped:
        When ``True`` the in-container build honours ``GODOT_BUILD_ARCHS`` so a
        single-platform Release run can restrict the arch matrix.
    deps:
        Names of ``deps/<dep>`` directories mounted into the container at
        ``/root/<dep>`` (order preserved for deterministic docker args).
    extra_env:
        Extra environment variables passed to the container for this platform.
    """

    name: str
    image_basename: str
    docker_required: bool = True
    arch_scoped: bool = False
    deps: tuple[str, ...] = ()
    extra_env: tuple[tuple[str, str], ...] = ()


# Canonical release build order — see module docstring.
PLATFORMS: tuple[Platform, ...] = (
    Platform(
        "linux",
        "godot-linux",
        docker_required=False,
        arch_scoped=True,
        deps=("accesskit",),
    ),
    Platform("android", "godot-android", deps=("swappy", "keystore")),
    Platform(
        "windows",
        "godot-windows",
        deps=("angle", "accesskit", "winrt"),
        extra_env=(("STEAM", "0"),),
    ),
    Platform("macos", "godot-osx", deps=("moltenvk", "angle", "accesskit")),
    Platform("ios", "godot-ios"),
    Platform("web", "godot-web"),
)

_BY_NAME: dict[str, Platform] = {p.name: p for p in PLATFORMS}


def get(name: str) -> Platform:
    """Return the :class:`Platform` for *name* (KeyError if unknown)."""
    return _BY_NAME[name]


def release_order() -> list[str]:
    """Platform names in canonical release build order."""
    return [p.name for p in PLATFORMS]


def names() -> frozenset[str]:
    """The set of all supported platform names."""
    return frozenset(_BY_NAME)


def docker_required_names() -> frozenset[str]:
    """Platforms that require Docker (no local SCons fallback)."""
    return frozenset(p.name for p in PLATFORMS if p.docker_required)


def arch_scoped_names() -> frozenset[str]:
    """Platforms whose in-container build honours ``GODOT_BUILD_ARCHS``."""
    return frozenset(p.name for p in PLATFORMS if p.arch_scoped)


def image_ref(name: str, registry: str, username: str, version: str) -> str:
    """Resolve the full container image reference for *name*.

    ``{registry}/{username}/{image_basename}:{version}`` — matches what
    ``containers --push`` produces and what ``config.toml`` declares.
    """
    return f"{registry}/{username}/{get(name).image_basename}:{version}"
