"""TOML configuration loader and validator for build-godot.py.

Requires Python 3.11+ for stdlib tomllib.
"""

from __future__ import annotations

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


def get_platform_config(config: dict, platform_name: str) -> dict | None:
    """Return the [[platforms]] entry whose *name* matches *platform_name*, or
    ``None`` if not found."""
    for entry in config.get("platforms", []):
        if entry.get("name") == platform_name:
            return entry
    return None
