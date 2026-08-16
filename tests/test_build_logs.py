"""Tests for scripts.build_logs."""

from __future__ import annotations

from pathlib import Path

from scripts import build_logs


def test_allocate_run_logs_dir_creates_latest_symlink(tmp_path):
    run_dir = build_logs.allocate_run_logs_dir(tmp_path)
    assert run_dir.is_dir()
    latest = tmp_path / "logs" / "latest"
    assert latest.is_symlink()
    assert latest.resolve() == run_dir.resolve()


def test_scons_log_path_honours_platform_arch_target(monkeypatch, tmp_path):
    monkeypatch.setenv("GODOT_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("GODOT_BUILD_PLATFORM", "linux")
    path = build_logs.scons_log_path(
        ("platform=linuxbsd", "arch=arm64", "target=editor", "module_mono_enabled=yes")
    )
    assert path == tmp_path / "linux" / "mono" / "arm64.editor.log"


def test_scons_log_path_mono_glue_uses_flat_name(monkeypatch, tmp_path):
    monkeypatch.setenv("GODOT_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("GODOT_BUILD_PLATFORM", "mono-glue")
    path = build_logs.scons_log_path(
        ("platform=linuxbsd", "target=editor", "module_mono_enabled=yes")
    )
    assert path == tmp_path / "mono-glue" / "scons.editor.log"


def test_gradle_log_path_uses_flavor(monkeypatch, tmp_path):
    monkeypatch.setenv("GODOT_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("GODOT_BUILD_PLATFORM", "android")
    monkeypatch.setenv("GODOT_BUILD_FLAVOR", "classical")
    path = build_logs.gradle_log_path("generateGodotTemplates")
    assert path == tmp_path / "android" / "classical" / "gradle.generateGodotTemplates.log"
