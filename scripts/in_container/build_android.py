"""In-container entry point for the Android build container.

Builds Android editor + templates (classical + Mono) across four arches
(arm32, arm64, x86_32, x86_64), then runs gradle to produce the artifacts:
the editor APK/AAB (standard + HorizonOS + PicoOS variants, with native debug
symbols) and the export-template APK/AAB/AAR + source zip.

Env contract:

  ============================  ==================================================
  Var                            Meaning
  ============================  ==================================================
  CLASSICAL                      Build classical artifacts when "1".
  MONO                           Build mono artifacts when "1".
  NUM_CORES                      ``scons -j``.
  GODOT_ANDROID_SIGN_KEYSTORE    Set when keystore is provided; otherwise the
  GODOT_ANDROID_SIGN_KEY_ALIAS   build produces an UNSIGNED debug build (downstream
  GODOT_ANDROID_SIGN_PASSWORD    app-export signing is handled by the Godot
                                 exporter, not by the engine template build).
  ============================  ==================================================

Notes:

  * Keystore graceful no-op — if the keystore config snippet is absent,
    the three GODOT_ANDROID_SIGN_* vars default to empty strings.
  * ``store_release=yes/no`` is computed from whether the keystore is
    populated; debug build otherwise.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from scripts.in_container import common

logger = logging.getLogger(__name__)


_OPTIONS: tuple[str, ...] = ("production=yes",)
_OPTIONS_MONO: tuple[str, ...] = (
    "module_mono_enabled=yes",
    "module_dotnet_enabled=yes",
)
_ARCHS: tuple[str, ...] = ("arm32", "arm64", "x86_32", "x86_64")

# Native debug symbols zip produced by SCsub during the last template_release
# arch build (see _release_symbol_flags). It is not one of the gradle
# apk/aab/aar outputs, so it needs its own copy step to survive container
# teardown. (gradle's cleanGodotTemplates would delete it, but
# generateGodotTemplates does not depend on that clean task.)
_NATIVE_SYMBOLS_ZIP = "android-template-release-native-symbols.zip"


def _release_symbol_flags(arch: str) -> tuple[str, ...]:
    """Return the native debug-symbol SCons flags for a template_release arch.

    Per the Godot "Resolving crashes on Android" guide
    (docs.godotengine.org/en/latest/tutorials/platform/android/resolving_crashes_on_android.html):

      * ``debug_symbols=yes`` is set on EVERY arch so each per-arch ``.so`` is
        compiled with symbols.
      * ``separate_debug_symbols=yes`` is added ONLY on the final arch
        (``_ARCHS[-1]``). That last build is when ``platform/android/SCsub``
        zips the accumulated ``platform/android/java/lib/libs`` tree — by then
        holding all four arches — into
        ``bin/android-template-release-native-symbols.zip``. This mirrors how
        the guide gates ``generate_android_binaries=yes`` to the last command.

    Because this build runs SCons manually and then invokes gradle with the
    scons tasks excluded (``excludeSconsBuildTasks()`` is true without
    ``-PgenerateNativeLibs``), gradle packages exactly these libs rather than
    recompiling them symbol-free.
    """
    flags = ["debug_symbols=yes"]
    if arch == _ARCHS[-1]:
        flags.append("separate_debug_symbols=yes")
    return tuple(flags)


def _copy_native_symbols(godot_dir: Path, dest: Path) -> None:
    """Copy ``bin/<_NATIVE_SYMBOLS_ZIP>`` into *dest* if it was produced.

    A missing zip is a WARNING, not fatal — other Android artifacts still
    publish. (It is absent only if the template_release matrix was built
    without the symbol flags.)
    """
    src = godot_dir / "bin" / _NATIVE_SYMBOLS_ZIP
    if not src.is_file():
        logger.warning(
            "Native debug symbols zip %s not found; skipping. (Was the "
            "template_release matrix built with debug_symbols + "
            "separate_debug_symbols?)",
            src,
        )
        return
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest / src.name)
    logger.info("Copied Android native debug symbols to %s", dest / src.name)


def _ensure_signing_env() -> None:
    """Keystore guard for the Android template build.

    The legacy ``/root/keystore/config.sh`` was removed by the PR #7 rework
    and is now a graceful no-op when no keystore is configured (the
    ``[signing]`` section of config.toml is currently empty), so
    ``/root/keystore`` is mounted empty into the container. When
    ``config.sh`` is absent we default the signing vars to empty strings
    and the build produces an UNSIGNED debug build.
    """
    cfg = Path("/root/keystore/config.sh")
    if cfg.is_file():
        logger.info(
            "Found %s — signing vars are expected to be exported by the parent process.",
            cfg,
        )
        return
    logger.info(
        "INFO: /root/keystore/config.sh not present; building unsigned Android templates."
    )
    for var in (
        "GODOT_ANDROID_SIGN_KEYSTORE",
        "GODOT_ANDROID_SIGN_KEY_ALIAS",
        "GODOT_ANDROID_SIGN_PASSWORD",
    ):
        os.environ.setdefault(var, "")


def _copy_template_outputs(godot_dir: Path, dest: Path, *, mono: bool) -> None:
    """Copy the gradle outputs (apk/aab/aar) into ``dest``."""
    dest.mkdir(parents=True, exist_ok=True)
    bin_dir = godot_dir / "bin"
    if mono:
        # Mono filenames are different from classical.
        pairs = (
            ("android_source.zip", "android_source.zip"),
            ("android_monoDebug.apk", "android_debug.apk"),
            ("android_monoRelease.apk", "android_release.apk"),
            ("godot-lib.template_release.aar", "godot-lib.template_release.aar"),
        )
    else:
        pairs = (
            ("android_source.zip", "android_source.zip"),
            ("android_debug.apk", "android_debug.apk"),
            ("android_release.apk", "android_release.apk"),
            ("godot-lib.template_release.aar", "godot-lib.template_release.aar"),
        )
    for src_name, dest_name in pairs:
        src = bin_dir / src_name
        if not src.is_file():
            logger.warning(
                "Android artifact %s missing under %s; skipping copy to %s.",
                src_name,
                bin_dir,
                dest_name,
            )
            continue
        shutil.copy2(src, dest / dest_name)


def _copy_editor_outputs(godot_dir: Path, dest: Path, *, store_release: str) -> None:
    """Copy the Android *editor* APK/AAB (+ HorizonOS/PicoOS APKs + native
    symbols) that ``generateGodotEditor`` produced into ``dest``.

    gradle names the artifacts ``android_editor-<platform>-<release|debug>.*``
    depending on whether a signing keystore was provided (``store_release``);
    the native symbols zip is likewise ``android-editor-<release|debug>-...``.
    Missing artifacts are a WARNING, not fatal — the templates still publish.
    """
    suffix = "release" if store_release == "yes" else "debug"
    dest.mkdir(parents=True, exist_ok=True)
    bin_dir = godot_dir / "bin"
    editor_builds = bin_dir / "android_editor_builds"
    pairs = (
        (
            bin_dir / f"android-editor-{suffix}-native-symbols.zip",
            "android_editor_native_debug_symbols.zip",
        ),
        (editor_builds / f"android_editor-android-{suffix}.apk", "android_editor.apk"),
        (editor_builds / f"android_editor-android-{suffix}.aab", "android_editor.aab"),
        (
            editor_builds / f"android_editor-horizonos-{suffix}.apk",
            "android_editor_horizonos.apk",
        ),
        (
            editor_builds / f"android_editor-picoos-{suffix}.apk",
            "android_editor_picoos.apk",
        ),
    )
    for src, dest_name in pairs:
        if not src.is_file():
            logger.warning(
                "Android editor artifact %s missing; skipping copy to %s.",
                src,
                dest_name,
            )
            continue
        shutil.copy2(src, dest / dest_name)
        logger.info("Copied Android editor artifact -> %s", dest / dest_name)


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    num_cores = common.env_num_cores()
    classical = common.env_flag("CLASSICAL")
    mono = common.env_flag("MONO")
    out_root = common.env_out_root()
    mono_glue_src = common.env_mono_glue_dir()
    swappy_src = Path(os.environ.get("SWAPPY_DIR") or "/root/swappy")
    env = dict(os.environ)
    env.setdefault("TERM", "xterm")

    try:
        godot_dir = common.setup_godot_source()
        common.apply_swappy(swappy_src, godot_dir)

        _ensure_signing_env()

        keystore = os.environ.get("GODOT_ANDROID_SIGN_KEYSTORE", "")
        if not keystore:
            logger.info(
                "No keystore provided to sign the Android release editor build, "
                "using debug build instead."
            )
            store_release = "no"
        else:
            store_release = "yes"
        env["GODOT_ANDROID_SIGN_KEYSTORE"] = os.environ.get(
            "GODOT_ANDROID_SIGN_KEYSTORE", ""
        )
        env["GODOT_ANDROID_SIGN_KEY_ALIAS"] = os.environ.get(
            "GODOT_ANDROID_SIGN_KEY_ALIAS", ""
        )
        env["GODOT_ANDROID_SIGN_PASSWORD"] = os.environ.get(
            "GODOT_ANDROID_SIGN_PASSWORD", ""
        )

        if classical:
            logger.info("Starting classical build for Android...")
            # Editor: build every arch with native debug symbols (the last arch,
            # x86_64, additionally emits the separate-symbols zip that covers all
            # editor archs — same gating as the templates pass).
            for arch in _ARCHS:
                common.run_scons(
                    "platform=android",
                    f"arch={arch}",
                    *_OPTIONS,
                    "target=editor",
                    f"store_release={store_release}",
                    *_release_symbol_flags(arch),
                    num_cores=num_cores,
                    env=env,
                    cwd=godot_dir,
                )

            # Assemble the editor APK/AAB: the standard Android editor plus the
            # HorizonOS and PicoOS variants, then copy the built artifacts out.
            common.gradle_wrapper(godot_dir, "generateGodotEditor")
            common.gradle_wrapper(godot_dir, "generateGodotHorizonOSEditor")
            common.gradle_wrapper(godot_dir, "generateGodotPicoOSEditor")
            _copy_editor_outputs(
                godot_dir, out_root / "tools", store_release=store_release
            )

            # Restart from a clean tarball, as we'll copy all the contents
            # outside the container for the MavenCentral upload.
            logger.info("Restarting from clean tarball for templates pass...")
            shutil.rmtree(godot_dir)
            godot_dir = common.setup_godot_source()
            common.apply_swappy(swappy_src, godot_dir)

            for arch in _ARCHS:
                common.run_scons(
                    "platform=android",
                    f"arch={arch}",
                    *_OPTIONS,
                    "target=template_debug",
                    num_cores=num_cores,
                    env=env,
                    cwd=godot_dir,
                )
                common.run_scons(
                    "platform=android",
                    f"arch={arch}",
                    *_OPTIONS,
                    "target=template_release",
                    *_release_symbol_flags(arch),
                    num_cores=num_cores,
                    env=env,
                    cwd=godot_dir,
                )

            common.gradle_wrapper(godot_dir, "generateGodotTemplates")
            _copy_native_symbols(godot_dir, out_root / "templates")

            if store_release == "yes":
                # Copy source folder with compiled libs so we can optionally
                # upload templates to MavenCentral.
                source_dest = out_root / "source"
                if source_dest.exists():
                    shutil.rmtree(source_dest)
                shutil.copytree(godot_dir, source_dest)
                gradle_home = Path("/root/.gradle")
                if gradle_home.is_dir():
                    shutil.copytree(
                        gradle_home, source_dest / ".gradle", dirs_exist_ok=True
                    )

            _copy_template_outputs(godot_dir, out_root / "templates", mono=False)

        if mono:
            logger.info("Starting Mono build for Android...")
            common.copy_mono_glue(mono_glue_src, godot_dir, include_editor=False)

            for arch in _ARCHS:
                common.run_scons(
                    "platform=android",
                    f"arch={arch}",
                    *_OPTIONS,
                    *_OPTIONS_MONO,
                    "target=template_debug",
                    num_cores=num_cores,
                    env=env,
                    cwd=godot_dir,
                )
                common.run_scons(
                    "platform=android",
                    f"arch={arch}",
                    *_OPTIONS,
                    *_OPTIONS_MONO,
                    "target=template_release",
                    *_release_symbol_flags(arch),
                    num_cores=num_cores,
                    env=env,
                    cwd=godot_dir,
                )

            common.gradle_wrapper(godot_dir, "generateGodotMonoTemplates")
            _copy_native_symbols(godot_dir, out_root / "templates-mono")
            _copy_template_outputs(godot_dir, out_root / "templates-mono", mono=True)

        logger.info("Android build successful")
        return 0
    except common.InContainerBuildError as exc:
        logger.error("%s", exc)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
