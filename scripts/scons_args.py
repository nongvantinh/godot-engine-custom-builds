"""Derive SCons argument strings from the flavor x kind x mono x arch matrix.

This module is the single place where the §5.1 flavor->SCons mapping lives, so
it can be retuned without touching the CLI or the config schema. It is pure
(no I/O, no subprocess) and therefore directly unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Flavor -> SCons mapping (§5.1).
#
# Each flavor expands to:
#   - the SCons `target` family used when building templates
#       (`template_debug` / `template_release`)
#   - a set of extra option flags appended to the invocation
#
# The `editor` kind always builds `target=editor`; the flavor only controls the
# extra flags (production / dev_build / debug_symbols) in that case.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlavorMapping:
    """Resolved SCons settings for a single flavor."""

    # Template SCons targets this flavor produces (for kind=templates).
    template_targets: tuple[str, ...]
    # Extra SCons option flags appended to every invocation of this flavor.
    extra_flags: tuple[str, ...] = field(default_factory=tuple)


FLAVOR_MAP: dict[str, FlavorMapping] = {
    # Optimized, no dev/debug symbols.
    "release": FlavorMapping(
        template_targets=("template_release",),
        extra_flags=("production=yes",),
    ),
    # Dev build with debug symbols.
    "debug": FlavorMapping(
        template_targets=("template_debug",),
        extra_flags=("dev_build=yes", "debug_symbols=yes"),
    ),
    # Optimized binary that keeps debug symbols (profiling/debuggable release).
    # production=yes is intentionally left off.
    "release_debug": FlavorMapping(
        template_targets=("template_release",),
        extra_flags=("debug_symbols=yes",),
    ),
}

VALID_FLAVORS = tuple(FLAVOR_MAP.keys())
VALID_KINDS = ("editor", "templates")
VALID_MONO = ("on", "off")


class SconsDerivationError(Exception):
    """Raised when an invalid flavor/kind/mono value is requested."""


@dataclass(frozen=True)
class SconsInvocation:
    """A single resolved SCons command (without the leading `scons`)."""

    platform_flags: str
    arch: str
    target: str
    mono: bool
    flavor: str
    kind: str
    options: tuple[str, ...]

    def to_args(self) -> list[str]:
        """Return the ordered SCons argument tokens for this invocation."""
        args: list[str] = []
        if self.platform_flags:
            args.extend(self.platform_flags.split())
        args.append(f"arch={self.arch}")
        args.append(f"target={self.target}")
        args.extend(self.options)
        return args

    def to_command(self) -> str:
        """Return the SCons args joined into a single string for logging."""
        return " ".join(self.to_args())


def _mono_options() -> tuple[str, ...]:
    """SCons option tokens enabling the Mono/.NET module (§4.2)."""
    return ("module_mono_enabled=yes", "module_dotnet_enabled=yes")


def derive_invocations(
    *,
    platform_flags: str,
    archs: list[str],
    flavors: list[str],
    kinds: list[str],
    mono_variants: list[str],
    accesskit_sdk_path: str | None = None,
    redirect_build_objects: bool = False,
    extra_flags: str = "",
) -> list[SconsInvocation]:
    """Expand the requested matrix into a flat list of SCons invocations.

    Parameters
    ----------
    platform_flags:
        Platform-invariant SCons flags (e.g. ``platform=linuxbsd``).
    archs:
        Architectures to build for this platform.
    flavors:
        Subset of :data:`VALID_FLAVORS`.
    kinds:
        Subset of :data:`VALID_KINDS`.
    mono_variants:
        Subset of :data:`VALID_MONO` (``on``/``off``).
    accesskit_sdk_path:
        When set, appended as ``accesskit_sdk_path=...`` to classical builds.
    redirect_build_objects:
        When ``True``, append ``redirect_build_objects=no`` to every invocation.
    extra_flags:
        Raw flags appended verbatim to every invocation.

    Returns
    -------
    list[SconsInvocation]
        The fully-expanded set of SCons commands, deduplicated while preserving
        order (a flavor that maps to the same template target as another is not
        duplicated for the editor kind).
    """
    for flavor in flavors:
        if flavor not in FLAVOR_MAP:
            raise SconsDerivationError(
                f"Unknown flavor '{flavor}'. Valid flavors: {list(VALID_FLAVORS)}."
            )
    for kind in kinds:
        if kind not in VALID_KINDS:
            raise SconsDerivationError(
                f"Unknown kind '{kind}'. Valid kinds: {list(VALID_KINDS)}."
            )
    for variant in mono_variants:
        if variant not in VALID_MONO:
            raise SconsDerivationError(
                f"Unknown mono variant '{variant}'. Valid: {list(VALID_MONO)}."
            )

    invocations: list[SconsInvocation] = []
    seen: set[tuple] = set()

    for variant in mono_variants:
        is_mono = variant == "on"
        for arch in archs:
            for flavor in flavors:
                mapping = FLAVOR_MAP[flavor]
                # Resolve the SCons targets for each requested kind.
                targets: list[str] = []
                for kind in kinds:
                    if kind == "editor":
                        targets.append("editor")
                    else:  # templates
                        targets.extend(mapping.template_targets)

                for target in targets:
                    options = _assemble_options(
                        mapping=mapping,
                        is_mono=is_mono,
                        accesskit_sdk_path=accesskit_sdk_path,
                        redirect_build_objects=redirect_build_objects,
                        extra_flags=extra_flags,
                    )
                    inv = SconsInvocation(
                        platform_flags=platform_flags,
                        arch=arch,
                        target=target,
                        mono=is_mono,
                        flavor=flavor,
                        kind="editor" if target == "editor" else "templates",
                        options=options,
                    )
                    dedup_key = (arch, target, is_mono, options)
                    if dedup_key in seen:
                        continue
                    seen.add(dedup_key)
                    invocations.append(inv)

    return invocations


def _assemble_options(
    *,
    mapping: FlavorMapping,
    is_mono: bool,
    accesskit_sdk_path: str | None,
    redirect_build_objects: bool,
    extra_flags: str,
) -> tuple[str, ...]:
    """Build the ordered tuple of SCons option flags for one invocation."""
    options: list[str] = []
    if redirect_build_objects:
        options.append("redirect_build_objects=no")
    options.extend(mapping.extra_flags)
    # AccessKit applies to classical builds (the production desktop path).
    if accesskit_sdk_path and "production=yes" in mapping.extra_flags:
        options.append(f"accesskit_sdk_path={accesskit_sdk_path}")
    if is_mono:
        options.extend(_mono_options())
    if extra_flags.strip():
        options.extend(extra_flags.split())
    return tuple(options)
