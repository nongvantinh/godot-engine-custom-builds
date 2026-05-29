"""TOML configuration loader and validator for build-godot.py.

Requires Python 3.11+ for stdlib tomllib.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path


class ConfigError(Exception):
    """Raised when config.toml is missing required keys or contains forbidden keys."""


# Keys that must never appear in config.toml (secrets policy).
_FORBIDDEN_KEYS = {"ghcr_pat", "token"}

# Required top-level string keys.
_REQUIRED_TOPLEVEL = {"registry", "username", "godot_version"}

# Required keys inside each [[platforms]] entry.
_REQUIRED_PLATFORM_KEYS = {"name", "image", "scons_flags"}

# ---------------------------------------------------------------------------
# Defaults for the [build] matrix (§3 schema).
# ---------------------------------------------------------------------------

DEFAULT_FLAVORS = ["release", "debug", "release_debug"]
DEFAULT_KINDS = ["editor", "templates"]
DEFAULT_MONO = ["on", "off"]


def _default_build_jobs() -> int:
    """SCons ``-j`` default: leave 2 cores for the system (floor of 1).

    Dynamic so the value tracks the host's core count rather than a hardcoded
    number — on a 16-core host this resolves to 14. Falls back to 4 cores when
    ``os.cpu_count()`` returns ``None``.
    """
    return max(1, (os.cpu_count() or 4) - 2)


# SCons -j default: nproc - 2 (leave 2 cores for the system; floor of 1).
DEFAULT_BUILD_JOBS = _default_build_jobs()

_VALID_FLAVORS = {"release", "debug", "release_debug"}
_VALID_KINDS = {"editor", "templates"}
_VALID_MONO = {"on", "off"}

# Xcode / Apple SDK version strings the operator's Xcode_*.xip ships.
# Parameterised so the Apple Dockerfiles are not hand-edited per Xcode bump.
DEFAULT_XCODE_SDKV = "26.1.1"
DEFAULT_APPLE_SDKV = "26.1"

# Per-platform full arch matrix used when a [[platforms]] entry omits `archs`.
DEFAULT_PLATFORM_ARCHS: dict[str, list[str]] = {
    "linux": ["x86_64", "x86_32", "arm64", "arm32"],
    "windows": ["x86_64", "x86_32", "arm64"],
    "macos": ["universal"],
    "web": ["wasm32"],
    "android": ["arm64", "arm32", "x86_64", "x86_32"],
    "ios": ["arm64"],
}

# ---------------------------------------------------------------------------
# Defaults for [scons] alignment keys (Phase A).
# ---------------------------------------------------------------------------

DEFAULT_ACCESSKIT_SDK_PATH = "/root/accesskit/accesskit-c"
DEFAULT_REDIRECT_BUILD_OBJECTS = False

# ---------------------------------------------------------------------------
# Defaults for the [release] table (Phase D).
# ---------------------------------------------------------------------------

DEFAULT_RELEASE_REPO = "nongvantinh/godot-build-scripts"
DEFAULT_RELEASE_AUTO_UPLOAD = True
DEFAULT_RELEASE_DRAFT = False
DEFAULT_RELEASE_PRERELEASE = True

# Source defaults.
DEFAULT_GIT_BRANCH = "4.7.dev1"
DEFAULT_GODOT_REPO = "nongvantinh/godot"


def load_config(path: str) -> dict:
    """Load and validate *path* as a TOML build configuration.

    Returns the parsed config dict on success.
    Raises :class:`ConfigError` for any validation failure.
    Exits with a clear message rather than a raw ``KeyError`` or ``TypeError``.
    """
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(
            f"Configuration file not found: {path}\n"
            "Copy config.toml.example to config.toml and fill in your values."
        )

    try:
        with open(config_path, "rb") as fh:
            config = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Failed to parse {path}: {exc}") from exc

    _check_no_secrets(config)
    _check_required_toplevel(config, path)
    _check_platforms(config, path)
    _check_build_section(config, path)
    _check_release_section(config, path)

    return config


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _check_no_secrets(data: dict, path: str = "") -> None:
    """Recursively reject forbidden keys anywhere in the config tree.

    Parameters
    ----------
    data:
        The (sub-)dict to inspect.
    path:
        Dot-separated key path of *data* within the root config, used for
        error messages (e.g. ``"signing"`` or ``"platforms[0]"``).
    """
    for key, value in data.items():
        full_key = f"{path}.{key}" if path else key
        if key in _FORBIDDEN_KEYS:
            raise ConfigError(
                f"Forbidden key '{full_key}' found in config.toml. "
                "Never store credentials in config files — "
                "use the GHCR_PAT environment variable instead."
            )
        if isinstance(value, dict):
            _check_no_secrets(value, full_key)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    _check_no_secrets(item, f"{full_key}[{i}]")


def _check_required_toplevel(config: dict, path: str) -> None:
    for key in _REQUIRED_TOPLEVEL:
        if key not in config:
            raise ConfigError(
                f"Missing required key '{key}' in {path}.\n"
                f"Add '{key} = \"...\"' to your config.toml."
            )
        if not isinstance(config[key], str):
            raise ConfigError(
                f"Key '{key}' in {path} must be a string, "
                f"got {type(config[key]).__name__}."
            )

    # Optional source keys must be strings when present.
    for key in ("git_branch", "godot_repo"):
        if key in config and not isinstance(config[key], str):
            raise ConfigError(
                f"Key '{key}' in {path} must be a string, "
                f"got {type(config[key]).__name__}."
            )


def _check_platforms(config: dict, path: str) -> None:
    platforms = config.get("platforms")
    if not platforms:
        raise ConfigError(
            f"No [[platforms]] entries found in {path}.\n"
            "Define at least one [[platforms]] entry with 'name', 'image', "
            "and 'scons_flags'."
        )
    if not isinstance(platforms, list):
        raise ConfigError(
            f"'platforms' in {path} must be an array of tables ([[platforms]])."
        )
    for i, entry in enumerate(platforms):
        for key in _REQUIRED_PLATFORM_KEYS:
            if key not in entry:
                raise ConfigError(
                    f"[[platforms]] entry #{i + 1} in {path} is missing "
                    f"required key '{key}'."
                )
        archs = entry.get("archs")
        if archs is not None and (
            not isinstance(archs, list) or not all(isinstance(a, str) for a in archs)
        ):
            raise ConfigError(
                f"[[platforms]] entry #{i + 1} ('{entry.get('name')}') in {path} "
                "has an invalid 'archs' value — it must be an array of strings."
            )


def _check_build_section(config: dict, path: str) -> None:
    """Validate the optional [build] matrix section."""
    build = config.get("build")
    if build is None:
        return
    if not isinstance(build, dict):
        raise ConfigError(f"[build] in {path} must be a table.")

    _check_str_subset(build, "flavors", _VALID_FLAVORS, path)
    _check_str_subset(build, "kinds", _VALID_KINDS, path)
    _check_str_subset(build, "mono", _VALID_MONO, path)

    if "build_jobs" in build and not isinstance(build["build_jobs"], int):
        raise ConfigError(
            f"[build].build_jobs in {path} must be an integer, "
            f"got {type(build['build_jobs']).__name__}."
        )
    for key in ("xcode_sdkv", "apple_sdkv"):
        if key in build and not isinstance(build[key], str):
            raise ConfigError(f"[build].{key} in {path} must be a string.")


def _check_str_subset(table: dict, key: str, allowed: set[str], path: str) -> None:
    if key not in table:
        return
    value = table[key]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"[build].{key} in {path} must be an array of strings.")
    invalid = [v for v in value if v not in allowed]
    if invalid:
        raise ConfigError(
            f"[build].{key} in {path} contains invalid value(s) "
            f"{invalid}. Allowed: {sorted(allowed)}."
        )


def _check_release_section(config: dict, path: str) -> None:
    """Validate the optional [release] table."""
    release = config.get("release")
    if release is None:
        return
    if not isinstance(release, dict):
        raise ConfigError(f"[release] in {path} must be a table.")

    if "tag" in release:
        raise ConfigError(
            f"[release].tag in {path} is not configurable: the release tag is "
            f"derived from upstream/godot/version.py (e.g. '4.7.beta') so it "
            f"cannot drift from the engine binary. Remove this key from your "
            f"config.toml. Use the `--tag` CLI flag on the release sub-command "
            f"for one-off hotfix overrides."
        )
    if "repo" in release and not isinstance(release["repo"], str):
        raise ConfigError(f"[release].repo in {path} must be a string.")
    for key in ("auto_upload", "draft", "prerelease"):
        if key in release and not isinstance(release[key], bool):
            raise ConfigError(f"[release].{key} in {path} must be a boolean.")


# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------


def get_platform_config(config: dict, platform_name: str) -> dict | None:
    """Return the [[platforms]] entry whose *name* matches *platform_name*, or
    ``None`` if not found."""
    for entry in config.get("platforms", []):
        if entry.get("name") == platform_name:
            return entry
    return None


def get_platform_archs(config: dict, platform_name: str) -> list[str]:
    """Return the arch list for *platform_name*.

    Resolution order: the entry's ``archs`` key, then the per-platform default
    matrix, then an empty list for unknown platforms.
    """
    entry = get_platform_config(config, platform_name)
    if entry and entry.get("archs"):
        return list(entry["archs"])
    return list(DEFAULT_PLATFORM_ARCHS.get(platform_name, []))


def get_build_config(config: dict) -> dict:
    """Return the [build] matrix with defaults applied for any missing key."""
    build = config.get("build", {}) or {}
    return {
        "flavors": build.get("flavors", list(DEFAULT_FLAVORS)),
        "kinds": build.get("kinds", list(DEFAULT_KINDS)),
        "mono": build.get("mono", list(DEFAULT_MONO)),
        "build_jobs": build.get("build_jobs", DEFAULT_BUILD_JOBS),
        "xcode_sdkv": build.get("xcode_sdkv", DEFAULT_XCODE_SDKV),
        "apple_sdkv": build.get("apple_sdkv", DEFAULT_APPLE_SDKV),
    }


def get_scons_config(config: dict) -> dict:
    """Return the [scons] section with Phase-A defaults applied."""
    scons = config.get("scons", {}) or {}
    return {
        "use_lto": scons.get("use_lto", False),
        "extra_flags": scons.get("extra_flags", ""),
        "accesskit_sdk_path": scons.get(
            "accesskit_sdk_path", DEFAULT_ACCESSKIT_SDK_PATH
        ),
        "redirect_build_objects": scons.get(
            "redirect_build_objects", DEFAULT_REDIRECT_BUILD_OBJECTS
        ),
    }


def get_release_config(config: dict) -> dict:
    """Return the [release] table with defaults applied for any missing key."""
    release = config.get("release", {}) or {}
    # NOTE: no ``tag`` key — the release tag is derived from
    # ``upstream/godot/version.py`` in ``build-godot.py::cmd_release`` so it
    # cannot drift from what the engine binary reports. ``--tag`` on the
    # release sub-command remains available as a one-off override.
    return {
        "repo": release.get("repo", DEFAULT_RELEASE_REPO),
        "auto_upload": release.get("auto_upload", DEFAULT_RELEASE_AUTO_UPLOAD),
        "draft": release.get("draft", DEFAULT_RELEASE_DRAFT),
        "prerelease": release.get("prerelease", DEFAULT_RELEASE_PRERELEASE),
    }
