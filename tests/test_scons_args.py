"""Tests for scripts/scons_args.py — flavor x kind x mono x arch derivation."""

from __future__ import annotations

import pytest

from scripts.scons_args import (
    FLAVOR_MAP,
    SconsDerivationError,
    derive_invocations,
)

# ---------------------------------------------------------------------------
# Flavor -> SCons mapping (§5.1)
# ---------------------------------------------------------------------------


class TestFlavorMapping:
    def test_release_flavor_maps_to_template_release_and_production_when_resolved(self):
        mapping = FLAVOR_MAP["release"]

        assert mapping.template_targets == ("template_release",)
        assert "production=yes" in mapping.extra_flags

    def test_debug_flavor_maps_to_template_debug_with_dev_build_when_resolved(self):
        mapping = FLAVOR_MAP["debug"]

        assert mapping.template_targets == ("template_debug",)
        assert "dev_build=yes" in mapping.extra_flags
        assert "debug_symbols=yes" in mapping.extra_flags

    def test_release_debug_keeps_symbols_without_production_when_resolved(self):
        mapping = FLAVOR_MAP["release_debug"]

        assert mapping.template_targets == ("template_release",)
        assert "debug_symbols=yes" in mapping.extra_flags
        assert "production=yes" not in mapping.extra_flags


# ---------------------------------------------------------------------------
# Editor kind
# ---------------------------------------------------------------------------


class TestDeriveEditorKind:
    def test_editor_kind_always_targets_editor_when_flavor_release(self):
        invs = derive_invocations(
            platform_flags="platform=linuxbsd",
            archs=["x86_64"],
            flavors=["release"],
            kinds=["editor"],
            mono_variants=["off"],
        )

        assert len(invs) == 1
        assert invs[0].target == "editor"
        assert "arch=x86_64" in invs[0].to_args()

    def test_editor_invocation_includes_production_for_release_flavor(self):
        invs = derive_invocations(
            platform_flags="platform=linuxbsd",
            archs=["x86_64"],
            flavors=["release"],
            kinds=["editor"],
            mono_variants=["off"],
        )

        assert "production=yes" in invs[0].to_args()


# ---------------------------------------------------------------------------
# Templates kind
# ---------------------------------------------------------------------------


class TestDeriveTemplatesKind:
    def test_templates_kind_targets_template_release_for_release_flavor(self):
        invs = derive_invocations(
            platform_flags="platform=linuxbsd",
            archs=["x86_64"],
            flavors=["release"],
            kinds=["templates"],
            mono_variants=["off"],
        )

        assert [i.target for i in invs] == ["template_release"]

    def test_templates_kind_targets_template_debug_for_debug_flavor(self):
        invs = derive_invocations(
            platform_flags="platform=linuxbsd",
            archs=["x86_64"],
            flavors=["debug"],
            kinds=["templates"],
            mono_variants=["off"],
        )

        assert [i.target for i in invs] == ["template_debug"]

    def test_release_debug_editor_keeps_debug_symbols_without_production(self):
        invs = derive_invocations(
            platform_flags="platform=linuxbsd",
            archs=["x86_64"],
            flavors=["release_debug"],
            kinds=["editor"],
            mono_variants=["off"],
        )

        args = invs[0].to_args()
        assert "target=editor" in args
        assert "debug_symbols=yes" in args
        assert "production=yes" not in args

    def test_templates_kind_for_release_debug_uses_template_release(self):
        invs = derive_invocations(
            platform_flags="platform=linuxbsd",
            archs=["x86_64"],
            flavors=["release_debug"],
            kinds=["templates"],
            mono_variants=["off"],
        )

        assert [i.target for i in invs] == ["template_release"]
        assert "debug_symbols=yes" in invs[0].to_args()
        assert "production=yes" not in invs[0].to_args()

    def test_editor_and_templates_kinds_produce_distinct_targets_for_release(self):
        invs = derive_invocations(
            platform_flags="platform=linuxbsd",
            archs=["x86_64"],
            flavors=["release"],
            kinds=["editor", "templates"],
            mono_variants=["off"],
        )

        assert [i.target for i in invs] == ["editor", "template_release"]


# ---------------------------------------------------------------------------
# Mono toggle
# ---------------------------------------------------------------------------


class TestMonoToggle:
    def test_mono_on_adds_both_module_flags_when_requested(self):
        invs = derive_invocations(
            platform_flags="platform=linuxbsd",
            archs=["x86_64"],
            flavors=["release"],
            kinds=["editor"],
            mono_variants=["on"],
        )

        args = invs[0].to_args()
        assert "module_mono_enabled=yes" in args
        assert "module_dotnet_enabled=yes" in args

    def test_mono_off_omits_module_flags_when_requested(self):
        invs = derive_invocations(
            platform_flags="platform=linuxbsd",
            archs=["x86_64"],
            flavors=["release"],
            kinds=["editor"],
            mono_variants=["off"],
        )

        args = invs[0].to_args()
        assert "module_mono_enabled=yes" not in args
        assert "module_dotnet_enabled=yes" not in args

    @pytest.mark.parametrize("flavor", ["release", "debug", "release_debug"])
    @pytest.mark.parametrize("kind", ["editor", "templates"])
    def test_mono_module_flags_coexist_with_every_flavor_and_kind(self, flavor, kind):
        invs = derive_invocations(
            platform_flags="platform=linuxbsd",
            archs=["x86_64"],
            flavors=[flavor],
            kinds=[kind],
            mono_variants=["on"],
        )

        for inv in invs:
            args = inv.to_args()
            assert "module_mono_enabled=yes" in args
            assert "module_dotnet_enabled=yes" in args

    def test_both_mono_variants_double_the_invocation_count(self):
        off_only = derive_invocations(
            platform_flags="platform=linuxbsd",
            archs=["x86_64"],
            flavors=["release"],
            kinds=["editor"],
            mono_variants=["off"],
        )
        both = derive_invocations(
            platform_flags="platform=linuxbsd",
            archs=["x86_64"],
            flavors=["release"],
            kinds=["editor"],
            mono_variants=["on", "off"],
        )

        assert len(both) == 2 * len(off_only)


# ---------------------------------------------------------------------------
# Arch matrix
# ---------------------------------------------------------------------------


class TestArchMatrix:
    def test_each_arch_produces_its_own_invocation_when_multiple_archs(self):
        invs = derive_invocations(
            platform_flags="platform=linuxbsd",
            archs=["x86_64", "arm64"],
            flavors=["release"],
            kinds=["editor"],
            mono_variants=["off"],
        )

        archs = {i.arch for i in invs}
        assert archs == {"x86_64", "arm64"}


# ---------------------------------------------------------------------------
# Phase A alignment flags
# ---------------------------------------------------------------------------


class TestAlignmentFlags:
    def test_redirect_build_objects_no_emitted_when_enabled(self):
        invs = derive_invocations(
            platform_flags="platform=linuxbsd",
            archs=["x86_64"],
            flavors=["release"],
            kinds=["editor"],
            mono_variants=["off"],
            redirect_build_objects=True,
        )

        assert "redirect_build_objects=no" in invs[0].to_args()

    def test_redirect_build_objects_absent_when_disabled(self):
        invs = derive_invocations(
            platform_flags="platform=linuxbsd",
            archs=["x86_64"],
            flavors=["release"],
            kinds=["editor"],
            mono_variants=["off"],
            redirect_build_objects=False,
        )

        assert "redirect_build_objects=no" not in invs[0].to_args()

    def test_accesskit_path_added_to_classical_production_builds(self):
        invs = derive_invocations(
            platform_flags="platform=linuxbsd",
            archs=["x86_64"],
            flavors=["release"],
            kinds=["editor"],
            mono_variants=["off"],
            accesskit_sdk_path="/root/accesskit/accesskit-c",
        )

        assert "accesskit_sdk_path=/root/accesskit/accesskit-c" in invs[0].to_args()

    def test_accesskit_path_omitted_for_debug_flavor_without_production(self):
        invs = derive_invocations(
            platform_flags="platform=linuxbsd",
            archs=["x86_64"],
            flavors=["debug"],
            kinds=["editor"],
            mono_variants=["off"],
            accesskit_sdk_path="/root/accesskit/accesskit-c",
        )

        assert "accesskit_sdk_path=/root/accesskit/accesskit-c" not in invs[0].to_args()

    def test_extra_flags_appended_verbatim_when_provided(self):
        invs = derive_invocations(
            platform_flags="platform=linuxbsd",
            archs=["x86_64"],
            flavors=["release"],
            kinds=["editor"],
            mono_variants=["off"],
            extra_flags="use_lto=yes",
        )

        assert "use_lto=yes" in invs[0].to_args()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestDerivationValidation:
    def test_raises_when_flavor_is_unknown(self):
        with pytest.raises(SconsDerivationError, match="flavor"):
            derive_invocations(
                platform_flags="platform=linuxbsd",
                archs=["x86_64"],
                flavors=["nonsense"],
                kinds=["editor"],
                mono_variants=["off"],
            )

    def test_raises_when_kind_is_unknown(self):
        with pytest.raises(SconsDerivationError, match="kind"):
            derive_invocations(
                platform_flags="platform=linuxbsd",
                archs=["x86_64"],
                flavors=["release"],
                kinds=["nonsense"],
                mono_variants=["off"],
            )

    def test_raises_when_mono_variant_is_unknown(self):
        with pytest.raises(SconsDerivationError, match="mono"):
            derive_invocations(
                platform_flags="platform=linuxbsd",
                archs=["x86_64"],
                flavors=["release"],
                kinds=["editor"],
                mono_variants=["maybe"],
            )


# ---------------------------------------------------------------------------
# Full matrix expansion
# ---------------------------------------------------------------------------


class TestFullMatrix:
    def test_full_matrix_expands_to_expected_command_count_for_linux(self):
        # 4 archs x 3 flavors x (1 editor + 1 template target) x 2 mono variants.
        invs = derive_invocations(
            platform_flags="platform=linuxbsd",
            archs=["x86_64", "x86_32", "arm64", "arm32"],
            flavors=["release", "debug", "release_debug"],
            kinds=["editor", "templates"],
            mono_variants=["on", "off"],
        )

        # 4 archs * 3 flavors * 2 kinds * 2 mono = 48.
        assert len(invs) == 48
