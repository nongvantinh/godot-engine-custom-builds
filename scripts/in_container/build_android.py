"""In-container entry point for the Android build container.

Builds Android editor + templates (classical + Mono) across four arches
(arm32, arm64, x86_32, x86_64), then runs gradle to produce the APK/AAB/AAR
artifacts.

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
            for arch in _ARCHS:
                common.run_scons(
                    "platform=android",
                    f"arch={arch}",
                    *_OPTIONS,
                    "target=editor",
                    f"store_release={store_release}",
                    num_cores=num_cores,
                    env=env,
                    cwd=godot_dir,
                )

            common.gradle_wrapper(godot_dir, "generateGodotEditor")
            (out_root / "tools").mkdir(parents=True, exist_ok=True)

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
                    num_cores=num_cores,
                    env=env,
                    cwd=godot_dir,
                )

            common.gradle_wrapper(godot_dir, "generateGodotTemplates")

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
                    num_cores=num_cores,
                    env=env,
                    cwd=godot_dir,
                )

            common.gradle_wrapper(godot_dir, "generateGodotMonoTemplates")
            _copy_template_outputs(godot_dir, out_root / "templates-mono", mono=True)

        logger.info("Android build successful")
        return 0
    except common.InContainerBuildError as exc:
        logger.error("%s", exc)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
