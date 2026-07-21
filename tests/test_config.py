"""Tests for scripts/config.py — TOML configuration loader and validator."""

from __future__ import annotations

import pytest

from unittest import mock

from scripts.config import (
    DEFAULT_BUILD_JOBS,
    DEFAULT_PLATFORM_ARCHS,
    ConfigError,
    _default_build_jobs,
    get_build_config,
    get_platform_archs,
    get_platform_config,
    get_release_config,
    get_scons_config,
    load_config,
)

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
            _MINIMAL_VALID_TOML
            + '\n[scons]\nuse_lto = true\nextra_flags = "lto=full"\n',
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

    def test_raises_config_error_when_platform_entry_missing_scons_flags(
        self, tmp_path
    ):
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


# ---------------------------------------------------------------------------
# [build] matrix — defaults and validation (§3 schema)
# ---------------------------------------------------------------------------


class TestBuildSectionDefaults:
    def test_get_build_config_applies_defaults_when_section_absent(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        _write_toml(toml_file, _MINIMAL_VALID_TOML)
        cfg = load_config(str(toml_file))

        build = get_build_config(cfg)

        assert build["flavors"] == ["release", "debug", "release_debug"]
        assert build["kinds"] == ["editor", "templates"]
        assert build["mono"] == ["on", "off"]
        assert build["build_jobs"] == DEFAULT_BUILD_JOBS

    def test_get_build_config_reads_overrides_when_section_present(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        _write_toml(
            toml_file,
            _MINIMAL_VALID_TOML + "\n[build]\n"
            'flavors = ["release"]\n'
            'kinds = ["editor"]\n'
            'mono = ["on"]\n'
            "build_jobs = 4\n",
        )
        cfg = load_config(str(toml_file))

        build = get_build_config(cfg)

        assert build["flavors"] == ["release"]
        assert build["kinds"] == ["editor"]
        assert build["mono"] == ["on"]
        assert build["build_jobs"] == 4


class TestBuildSectionValidation:
    def test_raises_when_flavor_value_invalid(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        _write_toml(
            toml_file,
            _MINIMAL_VALID_TOML + '\n[build]\nflavors = ["nonsense"]\n',
        )

        with pytest.raises(ConfigError, match="flavors"):
            load_config(str(toml_file))

    def test_raises_when_mono_value_invalid(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        _write_toml(
            toml_file,
            _MINIMAL_VALID_TOML + '\n[build]\nmono = ["maybe"]\n',
        )

        with pytest.raises(ConfigError, match="mono"):
            load_config(str(toml_file))

    def test_raises_when_build_jobs_not_integer(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        _write_toml(
            toml_file,
            _MINIMAL_VALID_TOML + '\n[build]\nbuild_jobs = "ten"\n',
        )

        with pytest.raises(ConfigError, match="build_jobs"):
            load_config(str(toml_file))


class TestDefaultBuildJobs:
    """The build-jobs default is `nproc - 2` (floor of 1)."""

    def test_leaves_two_cores_on_a_16_core_host(self):
        with mock.patch("scripts.config.os.cpu_count", return_value=16):
            assert _default_build_jobs() == 14

    def test_floors_at_one_core(self):
        with mock.patch("scripts.config.os.cpu_count", return_value=2):
            assert _default_build_jobs() == 1
        with mock.patch("scripts.config.os.cpu_count", return_value=1):
            assert _default_build_jobs() == 1

    def test_falls_back_when_cpu_count_is_none(self):
        with mock.patch("scripts.config.os.cpu_count", return_value=None):
            # (4 fallback) - 2 = 2
            assert _default_build_jobs() == 2

    def test_default_is_dynamic_not_hardcoded(self):
        # The module-level default tracks the running host (nproc - 2, floor 1).
        import os

        assert DEFAULT_BUILD_JOBS == max(1, (os.cpu_count() or 4) - 2)


class TestAppleSdkVersionConfig:
    """[build].xcode_sdkv / apple_sdkv version strings used by container_builder."""

    def test_defaults_to_xcode_versions(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        _write_toml(toml_file, _MINIMAL_VALID_TOML)
        build = get_build_config(load_config(str(toml_file)))

        assert build["xcode_sdkv"] == "26.1.1"
        assert build["apple_sdkv"] == "26.1"

    def test_reads_overrides_when_present(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        _write_toml(
            toml_file,
            _MINIMAL_VALID_TOML + "\n[build]\n"
            'xcode_sdkv = "27.0"\n'
            'apple_sdkv = "27.0"\n',
        )
        build = get_build_config(load_config(str(toml_file)))

        assert build["xcode_sdkv"] == "27.0"
        assert build["apple_sdkv"] == "27.0"


# ---------------------------------------------------------------------------
# Per-platform archs
# ---------------------------------------------------------------------------


class TestPlatformArchs:
    def test_uses_explicit_archs_when_entry_defines_them(self, tmp_path):
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
archs = ["x86_64"]
""",
        )
        cfg = load_config(str(toml_file))

        assert get_platform_archs(cfg, "linux") == ["x86_64"]

    def test_falls_back_to_default_matrix_when_archs_absent(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        _write_toml(toml_file, _MINIMAL_VALID_TOML)
        cfg = load_config(str(toml_file))

        assert get_platform_archs(cfg, "linux") == DEFAULT_PLATFORM_ARCHS["linux"]

    def test_raises_when_archs_is_not_a_string_array(self, tmp_path):
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
archs = "x86_64"
""",
        )

        with pytest.raises(ConfigError, match="archs"):
            load_config(str(toml_file))


# ---------------------------------------------------------------------------
# [scons] alignment keys (Phase A)
# ---------------------------------------------------------------------------


class TestSconsSection:
    def test_scons_defaults_applied_when_section_absent(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        _write_toml(toml_file, _MINIMAL_VALID_TOML)
        cfg = load_config(str(toml_file))

        scons = get_scons_config(cfg)

        assert scons["accesskit_sdk_path"] == "/root/accesskit/accesskit-c"
        assert scons["redirect_build_objects"] is False

    def test_scons_alignment_keys_read_when_present(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        _write_toml(
            toml_file,
            _MINIMAL_VALID_TOML + "\n[scons]\n"
            'accesskit_sdk_path = "/custom/accesskit"\n'
            "redirect_build_objects = true\n",
        )
        cfg = load_config(str(toml_file))

        scons = get_scons_config(cfg)

        assert scons["accesskit_sdk_path"] == "/custom/accesskit"
        assert scons["redirect_build_objects"] is True


# ---------------------------------------------------------------------------
# [release] table (Phase D)
# ---------------------------------------------------------------------------


class TestReleaseSection:
    def test_release_defaults_applied_when_section_absent(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        _write_toml(toml_file, _MINIMAL_VALID_TOML)
        cfg = load_config(str(toml_file))

        release = get_release_config(cfg)

        # No "tag" key — release tag is derived from upstream/godot/version.py
        # in build-godot.py::cmd_release. The config can't override it (only
        # the --tag CLI flag can, for one-off hotfix overrides).
        assert "tag" not in release
        assert release["repo"] == "nongvantinh/godot-build-scripts"
        assert release["auto_upload"] is True
        assert release["prerelease"] is True
        assert release["draft"] is False
        # NuGet publishing defaults on, with an empty source (derived from
        # username in build-godot.py).
        assert release["publish_nuget"] is True
        assert release["nuget_source"] == ""

    def test_release_overrides_read_when_present(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        _write_toml(
            toml_file,
            _MINIMAL_VALID_TOML + "\n[release]\n"
            'repo = "owner/repo"\n'
            "auto_upload = false\n"
            "prerelease = false\n",
        )
        cfg = load_config(str(toml_file))

        release = get_release_config(cfg)

        assert "tag" not in release
        assert release["repo"] == "owner/repo"
        assert release["auto_upload"] is False
        assert release["prerelease"] is False

    def test_nuget_overrides_read_when_present(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        _write_toml(
            toml_file,
            _MINIMAL_VALID_TOML + "\n[release]\n"
            "publish_nuget = false\n"
            'nuget_source = "https://nuget.example/index.json"\n',
        )
        cfg = load_config(str(toml_file))

        release = get_release_config(cfg)

        assert release["publish_nuget"] is False
        assert release["nuget_source"] == "https://nuget.example/index.json"

    def test_raises_when_publish_nuget_not_bool(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        _write_toml(
            toml_file,
            _MINIMAL_VALID_TOML + '\n[release]\npublish_nuget = "yes"\n',
        )

        with pytest.raises(ConfigError, match="publish_nuget"):
            load_config(str(toml_file))

    def test_raises_when_nuget_source_not_str(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        _write_toml(
            toml_file,
            _MINIMAL_VALID_TOML + "\n[release]\nnuget_source = true\n",
        )

        with pytest.raises(ConfigError, match="nuget_source"):
            load_config(str(toml_file))

    def test_raises_when_tag_key_set_in_config(self, tmp_path):
        # The tag is derived from version.py and MUST NOT be set in config —
        # historic source of engine/template version drift.
        toml_file = tmp_path / "config.toml"
        _write_toml(
            toml_file,
            _MINIMAL_VALID_TOML + '\n[release]\ntag = "4.8.beta"\n',
        )

        with pytest.raises(ConfigError, match="not configurable"):
            load_config(str(toml_file))


# ---------------------------------------------------------------------------
# Secrets guard covers new tables
# ---------------------------------------------------------------------------


class TestSecretsGuardNewTables:
    def test_raises_when_token_nested_in_release_table(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        _write_toml(
            toml_file,
            _MINIMAL_VALID_TOML + '\n[release]\ntoken = "supersecret"\n',
        )

        with pytest.raises(ConfigError, match="token"):
            load_config(str(toml_file))

    def test_raises_when_ghcr_pat_nested_in_build_table(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        _write_toml(
            toml_file,
            _MINIMAL_VALID_TOML + '\n[build]\nghcr_pat = "supersecret"\n',
        )

        with pytest.raises(ConfigError, match="ghcr_pat"):
            load_config(str(toml_file))
