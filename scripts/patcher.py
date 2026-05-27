"""Patch system: apply custom source patches before building.

Patches are plain unified-diff files (*.patch) stored in the patches/
directory at the repository root. They are applied in lexicographic order
with ``git apply --directory=<source_dir>``.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def apply_patches(patches_dir: str, source_dir: str) -> None:
    """Apply all ``*.patch`` files found in *patches_dir* to *source_dir*.

    Patches are applied in lexicographic order so that a consistent naming
    convention (e.g. ``001-fix-foo.patch``, ``002-add-bar.patch``) controls
    application order.

    Parameters
    ----------
    patches_dir:
        Directory that contains ``*.patch`` files (typically ``patches/``).
    source_dir:
        The Godot source root the patches should be applied to.

    Notes
    -----
    * If *patches_dir* is empty or contains no ``*.patch`` files, the
      function is a no-op.
    * Raises ``subprocess.CalledProcessError`` if any patch fails to apply.
    """
    patches_path = Path(patches_dir)
    if not patches_path.is_dir():
        logger.debug("Patch directory %s does not exist; skipping.", patches_dir)
        return

    patch_files = sorted(patches_path.glob("*.patch"))
    if not patch_files:
        logger.debug("No *.patch files in %s; skipping.", patches_dir)
        return

    logger.info(
        "Applying %d patch(es) from %s to %s",
        len(patch_files),
        patches_dir,
        source_dir,
    )
    for patch_file in patch_files:
        logger.info("  Applying %s", patch_file.name)
        subprocess.run(
            ["git", "apply", "--directory", source_dir, str(patch_file)],
            check=True,
        )
        logger.debug("  Applied %s successfully.", patch_file.name)

    logger.info("All patches applied successfully.")
