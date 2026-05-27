"""Tests for scripts/config.py — TOML configuration loader and validator."""
from __future__ import annotations

import pytest

from scripts.config import ConfigError, get_platform_config, load_config

# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

_MINIMAL_VALID_TOML = """\
registry = "ghcr.io"
username = "testuser"
godot_version = "4.3"

[[platforms]]
name = "linux"
image = "ghcr.io/test/linux:4.3"
scons_flags = "platform=linuxbsd"
"""


def _write_toml(path, content: str) -> str:
    """Write *content* to *path* and return the str path."""
    path.write_text(content, encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# load_config — happy path
# ---------------------------------------------------------------------------


class TestLoadConfigHappyPath:
    def test_returns_dict_with_all_required_keys_when_config_is_valid(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        _write_toml(toml_file, _MINIMAL_VALID_TOML)

        cfg = load_config(str(toml_file))

        assert cfg["registry"] == "ghcr.io"
        assert cfg["username"] == "testuser"
        assert cfg["godot_version"] == "4.3"
        assert len(cfg["platforms"]) == 1

    def test_accepts_optional_scons_section_when_present(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        _write_toml(
            toml_file,
            _MINIMAL_VALID_TOML + '\n[scons]\nuse_lto = true\nextra_flags = "lto=full"\n',
        )

        cfg = load_config(str(toml_file))

        assert cfg["scons"]["use_lto"] is True
        assert cfg["scons"]["extra_flags"] == "lto=full"


# ---------------------------------------------------------------------------
# load_config — missing required top-level keys
# ---------------------------------------------------------------------------


class TestLoadConfigMissingRequiredKeys:
    def test_raises_config_error_when_registry_missing(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        _write_toml(
            toml_file,
            """\
username = "testuser"
godot_version = "4.3"

[[platforms]]
name = "linux"
image = "ghcr.io/test/linux:4.3"
scons_flags = "platform=linuxbsd"
""",
        )

        with pytest.raises(ConfigError, match="registry"):
            load_config(str(toml_file))

    def test_raises_config_error_when_username_missing(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        _write_toml(
            toml_file,
            """\
registry = "ghcr.io"
godot_version = "4.3"

[[platforms]]
name = "linux"
image = "ghcr.io/test/linux:4.3"
scons_flags = "platform=linuxbsd"
""",
        )

        with pytest.raises(ConfigError, match="username"):
            load_config(str(toml_file))

    def test_raises_config_error_when_godot_version_missing(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        _write_toml(
            toml_file,
            """\
registry = "ghcr.io"
username = "testuser"

[[platforms]]
name = "linux"
image = "ghcr.io/test/linux:4.3"
scons_flags = "platform=linuxbsd"
""",
        )

        with pytest.raises(ConfigError, match="godot_version"):
            load_config(str(toml_file))


# ---------------------------------------------------------------------------
# load_config — [[platforms]] validation
# ---------------------------------------------------------------------------


class TestLoadConfigPlatformsValidation:
    def test_raises_config_error_when_platform_entry_missing_name(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        _write_toml(
            toml_file,
            """\
registry = "ghcr.io"
username = "testuser"
godot_version = "4.3"

[[platforms]]
image = "ghcr.io/test/linux:4.3"
scons_flags = "platform=linuxbsd"
""",
        )

        with pytest.raises(ConfigError, match="name"):
            load_config(str(toml_file))

    def test_raises_config_error_when_no_platforms_defined(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        _write_toml(
            toml_file,
            """\
registry = "ghcr.io"
username = "testuser"
godot_version = "4.3"
""",
        )

        with pytest.raises(ConfigError):
            load_config(str(toml_file))

    def test_raises_config_error_when_platform_entry_missing_image(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        _write_toml(
            toml_file,
            """\
registry = "ghcr.io"
username = "testuser"
godot_version = "4.3"

[[platforms]]
name = "linux"
scons_flags = "platform=linuxbsd"
""",
        )

        with pytest.raises(ConfigError, match="image"):
            load_config(str(toml_file))

    def test_raises_config_error_when_platform_entry_missing_scons_flags(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        _write_toml(
            toml_file,
            """\
registry = "ghcr.io"
username = "testuser"
godot_version = "4.3"

[[platforms]]
name = "linux"
image = "ghcr.io/test/linux:4.3"
""",
        )

        with pytest.raises(ConfigError, match="scons_flags"):
            load_config(str(toml_file))


# ---------------------------------------------------------------------------
# load_config — secrets policy guard
# ---------------------------------------------------------------------------


class TestLoadConfigSecretsGuard:
    def test_raises_config_error_when_ghcr_pat_present_in_toml(self, tmp_path):
        # ghcr_pat must be a top-level key — place it before [[platforms]] so
        # TOML does not scope it under the last platform entry.
        toml_file = tmp_path / "config.toml"
        _write_toml(
            toml_file,
            """\
registry = "ghcr.io"
username = "testuser"
godot_version = "4.3"
ghcr_pat = "supersecret"

[[platforms]]
name = "linux"
image = "ghcr.io/test/linux:4.3"
scons_flags = "platform=linuxbsd"
""",
        )

        with pytest.raises(ConfigError, match="ghcr_pat"):
            load_config(str(toml_file))

    def test_raises_config_error_when_token_present_in_toml(self, tmp_path):
        # token must be a top-level key — place it before [[platforms]].
        toml_file = tmp_path / "config.toml"
        _write_toml(
            toml_file,
            """\
registry = "ghcr.io"
username = "testuser"
godot_version = "4.3"
token = "supersecret"

[[platforms]]
name = "linux"
image = "ghcr.io/test/linux:4.3"
scons_flags = "platform=linuxbsd"
""",
        )

        with pytest.raises(ConfigError, match="token"):
            load_config(str(toml_file))

    def test_raises_config_error_when_ghcr_pat_nested_under_signing(self, tmp_path):
        """ghcr_pat hidden inside [signing] sub-table must still be caught."""
        toml_file = tmp_path / "config.toml"
        _write_toml(
            toml_file,
            """\
registry = "ghcr.io"
username = "testuser"
godot_version = "4.3"

[signing]
ghcr_pat = "supersecret"

[[platforms]]
name = "linux"
image = "ghcr.io/test/linux:4.3"
scons_flags = "platform=linuxbsd"
""",
        )

        with pytest.raises(ConfigError, match="ghcr_pat"):
            load_config(str(toml_file))

    def test_raises_config_error_when_token_nested_in_platforms_entry(self, tmp_path):
        """token hidden inside a [[platforms]] entry must still be caught."""
        toml_file = tmp_path / "config.toml"
        _write_toml(
            toml_file,
            """\
registry = "ghcr.io"
username = "testuser"
godot_version = "4.3"

[[platforms]]
name = "linux"
image = "ghcr.io/test/linux:4.3"
scons_flags = "platform=linuxbsd"
token = "supersecret"
""",
        )

        with pytest.raises(ConfigError, match="token"):
            load_config(str(toml_file))


# ---------------------------------------------------------------------------
# load_config — file not found
# ---------------------------------------------------------------------------


class TestLoadConfigFileNotFound:
    def test_raises_config_error_when_file_does_not_exist(self, tmp_path):
        with pytest.raises(ConfigError, match="not found"):
            load_config(str(tmp_path / "nonexistent.toml"))


# ---------------------------------------------------------------------------
# get_platform_config
# ---------------------------------------------------------------------------


class TestGetPlatformConfig:
    def test_returns_matching_platform_entry_when_name_found(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        _write_toml(
            toml_file,
            """\
registry = "ghcr.io"
username = "testuser"
godot_version = "4.3"

[[platforms]]
name = "linux"
image = "ghcr.io/test/linux:4.3"
scons_flags = "platform=linuxbsd"

[[platforms]]
name = "windows"
image = "ghcr.io/test/windows:4.3"
scons_flags = "platform=windows"
""",
        )
        cfg = load_config(str(toml_file))

        entry = get_platform_config(cfg, "linux")

        assert entry is not None
        assert entry["name"] == "linux"
        assert entry["image"] == "ghcr.io/test/linux:4.3"
        assert entry["scons_flags"] == "platform=linuxbsd"

    def test_returns_none_when_platform_name_not_in_config(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        _write_toml(toml_file, _MINIMAL_VALID_TOML)
        cfg = load_config(str(toml_file))

        result = get_platform_config(cfg, "android")

        assert result is None

    def test_resolves_second_platform_correctly_when_multiple_defined(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        _write_toml(
            toml_file,
            """\
registry = "ghcr.io"
username = "testuser"
godot_version = "4.3"

[[platforms]]
name = "linux"
image = "ghcr.io/test/linux:4.3"
scons_flags = "platform=linuxbsd"

[[platforms]]
name = "windows"
image = "ghcr.io/test/windows:4.3"
scons_flags = "platform=windows"
""",
        )
        cfg = load_config(str(toml_file))

        entry = get_platform_config(cfg, "windows")

        assert entry is not None
        assert entry["name"] == "windows"
        assert entry["image"] == "ghcr.io/test/windows:4.3"
