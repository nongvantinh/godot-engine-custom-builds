"""Tests for scripts/patcher.py — patch application helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.patcher import apply_patches

# ---------------------------------------------------------------------------
# apply_patches — no-op cases
# ---------------------------------------------------------------------------


class TestApplyPatchesNoOp:
    def test_is_noop_when_patches_directory_does_not_exist(self, tmp_path):
        nonexistent = tmp_path / "patches"
        source_dir = str(tmp_path / "src")

        with patch("scripts.patcher.subprocess.run") as mock_run:
            apply_patches(str(nonexistent), source_dir)

        mock_run.assert_not_called()

    def test_is_noop_when_patches_directory_is_empty(self, tmp_path):
        patches_dir = tmp_path / "patches"
        patches_dir.mkdir()
        source_dir = str(tmp_path / "src")

        with patch("scripts.patcher.subprocess.run") as mock_run:
            apply_patches(str(patches_dir), source_dir)

        mock_run.assert_not_called()

    def test_is_noop_when_directory_contains_no_dot_patch_files(self, tmp_path):
        patches_dir = tmp_path / "patches"
        patches_dir.mkdir()
        (patches_dir / "README.txt").write_text("docs only — no patches")
        (patches_dir / "notes.md").write_text("# Notes")
        source_dir = str(tmp_path / "src")

        with patch("scripts.patcher.subprocess.run") as mock_run:
            apply_patches(str(patches_dir), source_dir)

        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# apply_patches — git apply invocation
# ---------------------------------------------------------------------------


class TestApplyPatchesCallsGitApply:
    def test_calls_git_apply_with_correct_arguments_for_single_patch(self, tmp_path):
        patches_dir = tmp_path / "patches"
        patches_dir.mkdir()
        patch_file = patches_dir / "001-fix-foo.patch"
        patch_file.write_text("--- a/foo\n+++ b/foo\n@@ -1 +1 @@\n-old\n+new\n")
        source_dir = str(tmp_path / "src")

        with patch("scripts.patcher.subprocess.run") as mock_run:
            apply_patches(str(patches_dir), source_dir)

        mock_run.assert_called_once_with(
            ["git", "apply", "--directory", source_dir, str(patch_file)],
            check=True,
        )

    def test_applies_patches_in_lexicographic_order(self, tmp_path):
        patches_dir = tmp_path / "patches"
        patches_dir.mkdir()
        # Create files out of alphabetical order to confirm sorting.
        (patches_dir / "003-third.patch").write_text("patch C")
        (patches_dir / "001-first.patch").write_text("patch A")
        (patches_dir / "002-second.patch").write_text("patch B")
        source_dir = str(tmp_path / "src")

        with patch("scripts.patcher.subprocess.run") as mock_run:
            apply_patches(str(patches_dir), source_dir)

        assert mock_run.call_count == 3
        applied_names = [Path(call[0][0][-1]).name for call in mock_run.call_args_list]
        assert applied_names == [
            "001-first.patch",
            "002-second.patch",
            "003-third.patch",
        ]

    def test_applies_all_patch_files_found_in_directory(self, tmp_path):
        patches_dir = tmp_path / "patches"
        patches_dir.mkdir()
        for i in range(1, 5):
            (patches_dir / f"00{i}-change.patch").write_text(f"patch {i}")
        source_dir = str(tmp_path / "src")

        with patch("scripts.patcher.subprocess.run") as mock_run:
            apply_patches(str(patches_dir), source_dir)

        assert mock_run.call_count == 4

    def test_skips_non_patch_files_when_mixed_with_dot_patch_files(self, tmp_path):
        patches_dir = tmp_path / "patches"
        patches_dir.mkdir()
        (patches_dir / "001-real.patch").write_text("real patch")
        (patches_dir / "README.txt").write_text("docs")
        (patches_dir / "notes.md").write_text("# Notes")
        source_dir = str(tmp_path / "src")

        with patch("scripts.patcher.subprocess.run") as mock_run:
            apply_patches(str(patches_dir), source_dir)

        assert mock_run.call_count == 1
        cmd = mock_run.call_args[0][0]
        assert "001-real.patch" in cmd[-1]

    def test_passes_source_dir_as_directory_argument_to_git_apply(self, tmp_path):
        patches_dir = tmp_path / "patches"
        patches_dir.mkdir()
        (patches_dir / "001-patch.patch").write_text("diff")
        custom_source = "/custom/godot/source"

        with patch("scripts.patcher.subprocess.run") as mock_run:
            apply_patches(str(patches_dir), custom_source)

        cmd = mock_run.call_args[0][0]
        assert "--directory" in cmd
        dir_idx = cmd.index("--directory")
        assert cmd[dir_idx + 1] == custom_source
