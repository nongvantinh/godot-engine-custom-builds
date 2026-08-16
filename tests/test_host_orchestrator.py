"""Tests for ``scripts/host_orchestrator.py`` — host-side build dispatcher.

Every external touchpoint is mocked so the tests never touch real Docker, the
network, the operator's git, or the filesystem outside ``tmp_path``:

  * ``subprocess.run`` / ``subprocess.Popen`` — docker, git, make_tarball.sh, 7z.
  * ``urllib.request.urlretrieve`` — dep downloads.
  * ``shutil.which`` — 7z binary detection.

The behaviour we cover here is the host-orchestrator contract:

  * Image refs resolve to ``${registry}/${username}/godot-<plat>:${version}``.
  * Per-platform resumability gate (depth ≥ 2) — populated dirs are skipped.
  * Mono-glue gate (depth ≥ 1) — populated dirs are skipped.
  * Apple targets are always yielded by ``_iter_platforms`` — failures surface
    inside the container, never as a host-side skip.
  * Partial-state retry — ``.7z`` present but no extracted dir triggers
    re-extract.
  * Curl retry on transient failure — first ``URLError`` then success.
  * ``prepare_source`` uses the LOCAL submodule (no clone, no reset/clean) and
    WARNs on a branch mismatch.
  * Chown failure is non-fatal (run still exits 0).
"""

from __future__ import annotations

import io
import logging
import subprocess
import sys
import tarfile
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from scripts import host_orchestrator


def _is_version_read_subprocess(cmd) -> bool:
    """Detect the subprocess call from :func:`_read_version`.

    ``_read_version`` reads ``version.py`` in a child ``python -c`` process
    for isolation. Tests that already mock ``subprocess.run`` need a way to
    recognise that specific call and delegate it to the real interpreter
    (the staged ``version.py`` is small and trustworthy under test).
    """
    return len(cmd) >= 2 and cmd[0] == sys.executable and cmd[1] == "-c"


def _real_version_read(cmd):
    """Reproduce ``_read_version``'s output by parsing the staged version.py.

    Tests that mock both ``subprocess.run`` AND ``subprocess.Popen`` cannot
    shell out to a real interpreter — ``subprocess.run`` internally
    constructs a ``Popen``, which the mock intercepts. Instead we parse the
    third ``cmd`` argument (the upstream directory) and read ``version.py``
    inline, returning a fake :class:`subprocess.CompletedProcess` that
    mirrors what ``_read_version`` expects (``stdout`` = two lines:
    version, status).
    """
    # cmd = [sys.executable, "-c", code, str(upstream_godot_dir)]
    upstream_dir = Path(cmd[3])
    namespace: dict = {}
    exec(
        compile(
            (upstream_dir / "version.py").read_text(encoding="utf-8"),
            str(upstream_dir / "version.py"),
            "exec",
        ),
        namespace,
    )
    major = namespace.get("major", 0)
    minor = namespace.get("minor", 0)
    patch = namespace.get("patch", 0)
    status = namespace.get("status", "")
    if patch:
        version_line = f"{major}.{minor}.{patch}"
    else:
        version_line = f"{major}.{minor}"
    return subprocess.CompletedProcess(
        cmd, 0, stdout=f"{version_line}\n{status}\n", stderr=""
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Mirrors the pins the host orchestrator parses out of the engine's
# ``misc/scripts/install_swappy_android.py`` (tag, archive name, ABI list).
_SWAPPY_INSTALL_SCRIPT = """\
swappy_tag = "{tag}"
swappy_filename = "godot-swappy.zip"
swappy_folder = "thirdparty/swappy-frame-pacing"
swappy_archs = [
    "arm64-v8a",
    "armeabi-v7a",
    "x86",
    "x86_64",
]
"""
_SWAPPY_ARCHS = ("arm64-v8a", "armeabi-v7a", "x86", "x86_64")


def _stage_swappy_libs(target: Path, archs=_SWAPPY_ARCHS) -> None:
    for arch in archs:
        (target / arch).mkdir(parents=True, exist_ok=True)
        (target / arch / "libswappy_static.a").write_text("lib")


def _make_version_py(
    upstream: Path,
    *,
    major: int = 4,
    minor: int = 8,
    patch: int = 0,
    status: str = "dev1",
) -> None:
    """Write a minimal ``version.py`` mirroring upstream Godot's format."""
    upstream.mkdir(parents=True, exist_ok=True)
    (upstream / "version.py").write_text(
        f"major = {major}\n"
        f"minor = {minor}\n"
        f"patch = {patch}\n"
        f'status = "{status}"\n',
        encoding="utf-8",
    )
    # Also stage misc/scripts/make_tarball.sh so the existence check passes.
    scripts = upstream / "misc" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "make_tarball.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    # Stage the engine dependency-pin scripts the host orchestrator reads as the
    # single source of truth for dep versions (see _engine_install_version).
    (scripts / "install_accesskit.py").write_text(
        'ac_version = "0.22.3"\n', encoding="utf-8"
    )
    (scripts / "install_angle.py").write_text(
        'angle_version = "chromium/7219"\n', encoding="utf-8"
    )
    (scripts / "install_winrt.py").write_text(
        'winrt_version = "72"\n', encoding="utf-8"
    )
    (scripts / "install_swappy_android.py").write_text(
        _SWAPPY_INSTALL_SCRIPT.format(tag="from-source-2025-01-31"), encoding="utf-8"
    )


def _populate_platform_output(out_plat: Path) -> None:
    """Drop a depth-≥-2 file under *out_plat* so the resumability gate fires."""
    (out_plat / "tools").mkdir(parents=True, exist_ok=True)
    (out_plat / "tools" / "godot.linuxbsd.editor.x86_64").write_text("binary")


# ---------------------------------------------------------------------------
# Image-ref resolution
# ---------------------------------------------------------------------------


class TestResolveImageNames:
    def test_resolves_six_platform_images_without_base_distro_suffix(self):
        images = host_orchestrator._resolve_image_names("ghcr.io", "nongvantinh", "4.8")

        assert images["linux"] == "ghcr.io/nongvantinh/godot-linux:4.8"
        assert images["windows"] == "ghcr.io/nongvantinh/godot-windows:4.8"
        assert images["macos"] == "ghcr.io/nongvantinh/godot-osx:4.8"
        assert images["android"] == "ghcr.io/nongvantinh/godot-android:4.8"
        assert images["web"] == "ghcr.io/nongvantinh/godot-web:4.8"
        assert images["ios"] == "ghcr.io/nongvantinh/godot-ios:4.8"
        # No legacy "-${BASE_DISTRO}" suffix.
        for ref in images.values():
            assert not ref.endswith("-")


# ---------------------------------------------------------------------------
# pull_images
# ---------------------------------------------------------------------------


class TestPullImages:
    @staticmethod
    def _fake_run(*, present: bool):
        """subprocess.run stub: `docker image inspect` reports present/absent."""

        def fake_run(cmd, *args, **kwargs):
            if cmd[:3] == ["docker", "image", "inspect"]:
                return subprocess.CompletedProcess(cmd, 0 if present else 1)
            if cmd[:2] == ["docker", "pull"]:
                return subprocess.CompletedProcess(cmd, 0)
            raise AssertionError(f"Unexpected subprocess call: {cmd}")

        return fake_run

    def test_pulls_every_image_when_absent_locally(self):
        images = host_orchestrator._resolve_image_names("ghcr.io", "u", "4.8")
        with mock.patch(
            "scripts.host_orchestrator.subprocess.run",
            side_effect=self._fake_run(present=False),
        ) as run:
            host_orchestrator._pull_images(images, dry_run=False)

        pulls = [c for c in run.call_args_list if c.args[0][:2] == ["docker", "pull"]]
        assert len(pulls) == len(images)

    def test_skips_pull_when_image_present_locally(self):
        """A locally present (e.g. retagged) image is used without pulling."""
        images = host_orchestrator._resolve_image_names("ghcr.io", "u", "4.8")
        with mock.patch(
            "scripts.host_orchestrator.subprocess.run",
            side_effect=self._fake_run(present=True),
        ) as run:
            host_orchestrator._pull_images(images, dry_run=False)

        pulls = [c for c in run.call_args_list if c.args[0][:2] == ["docker", "pull"]]
        assert pulls == []

    def test_platform_scope_limits_images_touched(self):
        images = host_orchestrator._resolve_image_names("ghcr.io", "u", "4.8")
        with mock.patch(
            "scripts.host_orchestrator.subprocess.run",
            side_effect=self._fake_run(present=False),
        ) as run:
            host_orchestrator._pull_images(images, platforms={"linux"}, dry_run=False)

        pulls = [c for c in run.call_args_list if c.args[0][:2] == ["docker", "pull"]]
        assert len(pulls) == 1
        assert pulls[0].args[0][2] == images["linux"]

    def test_pull_failure_raises(self):
        images = host_orchestrator._resolve_image_names("ghcr.io", "u", "4.8")

        def fake_run(cmd, *args, **kwargs):
            if cmd[:3] == ["docker", "image", "inspect"]:
                return subprocess.CompletedProcess(cmd, 1)
            return subprocess.CompletedProcess(cmd, 1)  # pull fails

        with mock.patch(
            "scripts.host_orchestrator.subprocess.run", side_effect=fake_run
        ):
            with pytest.raises(host_orchestrator._HostOrchestratorError):
                host_orchestrator._pull_images(
                    images, platforms={"linux"}, dry_run=False
                )

    def test_dry_run_does_not_invoke_subprocess(self):
        images = host_orchestrator._resolve_image_names("ghcr.io", "u", "4.8")
        with mock.patch("scripts.host_orchestrator.subprocess.run") as run:
            host_orchestrator._pull_images(images, dry_run=True)

        run.assert_not_called()


# ---------------------------------------------------------------------------
# Resumability gates
# ---------------------------------------------------------------------------


class TestPlatformHasArtifacts:
    def test_returns_false_for_missing_dir(self, tmp_path):
        assert host_orchestrator._platform_has_artifacts(tmp_path / "nope") is False

    def test_returns_false_for_empty_dir(self, tmp_path):
        d = tmp_path / "out" / "linux"
        d.mkdir(parents=True)
        assert host_orchestrator._platform_has_artifacts(d) is False

    def test_returns_false_for_files_at_depth_one_only(self, tmp_path):
        # Bash: -mindepth 2 -type f. A file directly in out/linux/ does NOT
        # count — only files inside a category subdir do.
        d = tmp_path / "out" / "linux"
        d.mkdir(parents=True)
        (d / "stray.txt").write_text("x")
        assert host_orchestrator._platform_has_artifacts(d) is False

    def test_returns_true_when_artifact_file_at_depth_two(self, tmp_path):
        d = tmp_path / "out" / "linux"
        _populate_platform_output(d)
        assert host_orchestrator._platform_has_artifacts(d) is True


class TestMonoGlueHasArtifacts:
    def test_returns_false_for_empty_dir(self, tmp_path):
        d = tmp_path / "mono-glue"
        d.mkdir()
        assert host_orchestrator._mono_glue_has_artifacts(d) is False

    def test_returns_true_when_any_file_present(self, tmp_path):
        # Mono glue: depth ≥ 1, mirroring `find ... -mindepth 1 -type f`.
        d = tmp_path / "mono-glue"
        (d / "GodotSharp" / "Generated").mkdir(parents=True)
        (d / "GodotSharp" / "Generated" / "thing.cs").write_text("x")
        assert host_orchestrator._mono_glue_has_artifacts(d) is True


# ---------------------------------------------------------------------------
# Source preparation
# ---------------------------------------------------------------------------


class TestPrepareSource:
    def test_uses_upstream_submodule_no_clone_no_reset(self, tmp_path):
        # The submodule is the LOCAL upstream/godot dir; we must not clone,
        # reset, clean, or switch branches. The only git invocation is the
        # rev-parse used to detect the current ref (read-only).
        basedir = tmp_path / "build-godot-and-templates"
        basedir.mkdir()
        upstream = tmp_path / "upstream" / "godot"
        _make_version_py(upstream)

        with mock.patch("scripts.host_orchestrator.subprocess.run") as run:

            def fake_run(cmd, *args, **kwargs):
                # The first git call is rev-parse --abbrev-ref HEAD.
                if cmd[:2] == ["git", "-C"] and "rev-parse" in cmd:
                    return subprocess.CompletedProcess(
                        cmd, 0, stdout="4.8.dev1\n", stderr=""
                    )
                # version.py read runs in a python -c subprocess for isolation.
                if _is_version_read_subprocess(cmd):
                    return _real_version_read(cmd)
                # make_tarball.sh — fake it producing the tarball alongside.
                if cmd[0] == "sh" and "make_tarball.sh" in cmd[1]:
                    (upstream.parent / "godot-4.8.tar.gz").write_text("tarball")
                    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
                # Any other subprocess call would mean we clone/reset — fail loudly.
                raise AssertionError(f"Unexpected subprocess call: {cmd}")

            run.side_effect = fake_run

            version, status = host_orchestrator._prepare_source(
                basedir=basedir,
                upstream_godot_dir=upstream,
                git_branch="4.8.dev1",
                version_status_patch="",
                dry_run=False,
            )

        # Tarball is moved next to *basedir*.
        assert (basedir / "godot-4.8.tar.gz").is_file()
        assert version == "4.8"
        assert status == "dev1"
        # No `git reset --hard` / `git clean -fdx` / `git clone` ever called.
        for call in run.call_args_list:
            cmd_list = call.args[0]
            assert "clone" not in cmd_list
            assert "reset" not in cmd_list
            assert "clean" not in cmd_list

    def test_warns_when_current_checkout_differs_from_git_branch(
        self, tmp_path, caplog
    ):
        basedir = tmp_path / "build-godot-and-templates"
        basedir.mkdir()
        upstream = tmp_path / "upstream" / "godot"
        _make_version_py(upstream)

        def fake_run(cmd, *args, **kwargs):
            if "rev-parse" in cmd:
                # Operator is on a feature branch, not the config's git_branch.
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="feature/my-fix\n", stderr=""
                )
            if _is_version_read_subprocess(cmd):
                return _real_version_read(cmd)
            if cmd[0] == "sh":
                (upstream.parent / "godot-4.8.tar.gz").write_text("tarball")
                return subprocess.CompletedProcess(cmd, 0)
            raise AssertionError(f"Unexpected subprocess call: {cmd}")

        caplog.set_level(logging.WARNING, logger="scripts.host_orchestrator")
        with mock.patch(
            "scripts.host_orchestrator.subprocess.run", side_effect=fake_run
        ):
            host_orchestrator._prepare_source(
                basedir=basedir,
                upstream_godot_dir=upstream,
                git_branch="4.8.dev1",
                version_status_patch="",
                dry_run=False,
            )

        text = caplog.text
        assert "feature/my-fix" in text
        assert "4.8.dev1" in text

    def test_computes_tarball_path_with_godot_version(self, tmp_path):
        basedir = tmp_path / "build-godot-and-templates"
        basedir.mkdir()
        upstream = tmp_path / "upstream" / "godot"
        _make_version_py(upstream, patch=2, status="stable")

        def fake_run(cmd, *args, **kwargs):
            if "rev-parse" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="4.8\n", stderr="")
            if _is_version_read_subprocess(cmd):
                return _real_version_read(cmd)
            if cmd[0] == "sh":
                # patch != 0 -> version becomes "4.8.2".
                (upstream.parent / "godot-4.8.2.tar.gz").write_text("tarball")
                return subprocess.CompletedProcess(cmd, 0)
            raise AssertionError(f"Unexpected: {cmd}")

        with mock.patch(
            "scripts.host_orchestrator.subprocess.run", side_effect=fake_run
        ):
            version, _ = host_orchestrator._prepare_source(
                basedir=basedir,
                upstream_godot_dir=upstream,
                git_branch="4.8",
                version_status_patch="",
                dry_run=False,
            )

        assert version == "4.8.2"
        assert (basedir / "godot-4.8.2.tar.gz").is_file()

    def test_dry_run_does_not_invoke_make_tarball(self, tmp_path):
        basedir = tmp_path / "build-godot-and-templates"
        basedir.mkdir()
        upstream = tmp_path / "upstream" / "godot"
        _make_version_py(upstream)

        def fake_run(cmd, *args, **kwargs):
            if "rev-parse" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="4.8.dev1\n", stderr=""
                )
            if _is_version_read_subprocess(cmd):
                return _real_version_read(cmd)
            raise AssertionError(f"Dry run should not subprocess for: {cmd}")

        with mock.patch(
            "scripts.host_orchestrator.subprocess.run", side_effect=fake_run
        ):
            version, status = host_orchestrator._prepare_source(
                basedir=basedir,
                upstream_godot_dir=upstream,
                git_branch="4.8.dev1",
                version_status_patch="",
                dry_run=True,
            )

        assert version == "4.8"
        assert status == "dev1"
        assert not (basedir / "godot-4.8.tar.gz").exists()


# ---------------------------------------------------------------------------
# _read_version runs in a subprocess for isolation
# ---------------------------------------------------------------------------


class TestReadVersionSubprocess:
    def test_invokes_subprocess_not_in_process_import(self, tmp_path):
        # The version.py read MUST run in a child python process so that a
        # tampered version.py cannot mutate the orchestrator's interpreter
        # state (sys.modules, atexit hooks, signal handlers, ...).
        upstream = tmp_path / "upstream" / "godot"
        _make_version_py(upstream)

        with mock.patch(
            "scripts.host_orchestrator.subprocess.run",
            wraps=subprocess.run,
        ) as run_spy:
            version, status = host_orchestrator._read_version(upstream)

        assert version == "4.8"
        assert status == "dev1"
        # At least one subprocess call, and it must invoke the python
        # interpreter with ``-c`` (i.e. the isolated read path, not the old
        # in-process importlib.exec_module).
        invocations = [call.args[0] for call in run_spy.call_args_list]
        assert any(
            _is_version_read_subprocess(cmd) for cmd in invocations
        ), f"Expected a python -c subprocess invocation, got: {invocations!r}"

    def test_subprocess_failure_raises_host_orchestrator_error(self, tmp_path):
        # Write a version.py that raises on import to simulate a corrupt /
        # tampered upstream submodule. The subprocess wrapper must surface
        # this as ``_HostOrchestratorError`` rather than letting the
        # CalledProcessError bubble up.
        upstream = tmp_path / "upstream" / "godot"
        upstream.mkdir(parents=True)
        (upstream / "version.py").write_text(
            'raise RuntimeError("tampered version.py")\n',
            encoding="utf-8",
        )

        with pytest.raises(host_orchestrator._HostOrchestratorError):
            host_orchestrator._read_version(upstream)


# ---------------------------------------------------------------------------
# Download + extract — partial state and retry
# ---------------------------------------------------------------------------


class TestDownloadWithRetry:
    def test_first_attempt_url_error_then_success(self, tmp_path, monkeypatch):
        # Speed up the test: collapse the backoff to ~0 sleep.
        monkeypatch.setattr(host_orchestrator, "_DOWNLOAD_BACKOFF_BASE_S", 0.0)
        dest = tmp_path / "x.zip"
        call_count = {"n": 0}

        def fake_urlretrieve(url, target):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # urlretrieve may leave a partial file; we simulate that.
                Path(target).write_text("partial")
                raise urllib.error.URLError("connection reset")
            Path(target).write_text("complete")
            return target, None

        with mock.patch(
            "scripts.host_orchestrator.urllib.request.urlretrieve",
            side_effect=fake_urlretrieve,
        ):
            host_orchestrator._download_with_retry("https://example.test/x.zip", dest)

        assert call_count["n"] == 2
        assert dest.is_file()
        assert dest.read_text() == "complete"

    def test_all_attempts_fail_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(host_orchestrator, "_DOWNLOAD_BACKOFF_BASE_S", 0.0)
        monkeypatch.setattr(host_orchestrator, "_DOWNLOAD_MAX_ATTEMPTS", 2)

        with mock.patch(
            "scripts.host_orchestrator.urllib.request.urlretrieve",
            side_effect=urllib.error.URLError("dns lookup failed"),
        ):
            with pytest.raises(host_orchestrator._HostOrchestratorError):
                host_orchestrator._download_with_retry(
                    "https://example.test/x.zip", tmp_path / "x.zip"
                )


class TestDownloadSwappy:
    @staticmethod
    def _upstream_with_swappy(tmp_path, tag="from-source-2025-01-31"):
        up = tmp_path / "upstream" / "godot"
        scripts = up / "misc" / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        (scripts / "install_swappy_android.py").write_text(
            _SWAPPY_INSTALL_SCRIPT.format(tag=tag), encoding="utf-8"
        )
        return up

    def test_skips_when_every_arch_lib_present_and_marker_current(self, tmp_path):
        # Skip only when every engine-listed ABI has its static lib AND the
        # version marker matches the pinned tag — a pure skip (no download).
        deps = tmp_path / "deps"
        target = deps / "swappy"
        _stage_swappy_libs(target)
        (target / ".dep-version").write_text("from-source-2025-01-31")
        up = self._upstream_with_swappy(tmp_path)

        with (
            mock.patch("scripts.host_orchestrator._download_with_retry") as dl,
            mock.patch("scripts.host_orchestrator._extract") as extract,
        ):
            host_orchestrator._download_swappy(deps, up, dry_run=False)

        dl.assert_not_called()
        extract.assert_not_called()

    def test_redownloads_when_an_arch_lib_is_missing(self, tmp_path):
        # A marked dir that is missing even one ABI's lib is not a usable
        # Swappy install; detect_swappy() would disable frame pacing.
        deps = tmp_path / "deps"
        target = deps / "swappy"
        _stage_swappy_libs(target, archs=("arm64-v8a",))
        (target / ".dep-version").write_text("from-source-2025-01-31")
        up = self._upstream_with_swappy(tmp_path)

        with (
            mock.patch("scripts.host_orchestrator._download_with_retry") as dl,
            mock.patch(
                "scripts.host_orchestrator._extract",
                side_effect=lambda archive, dest: _stage_swappy_libs(dest),
            ),
        ):
            host_orchestrator._download_swappy(deps, up, dry_run=False)

        dl.assert_called_once()

    def test_redownloads_when_marker_missing_or_stale(self, tmp_path):
        # A present-but-unmarked (or stale) dir is replaced and re-fetched at
        # the engine-pinned tag, then re-marked.
        deps = tmp_path / "deps"
        target = deps / "swappy"
        _stage_swappy_libs(target)  # present, but no marker
        up = self._upstream_with_swappy(tmp_path)

        with (
            mock.patch("scripts.host_orchestrator._download_with_retry") as dl,
            mock.patch(
                "scripts.host_orchestrator._extract",
                side_effect=lambda archive, dest: _stage_swappy_libs(dest),
            ) as extract,
        ):
            host_orchestrator._download_swappy(deps, up, dry_run=False)

        dl.assert_called_once()
        extract.assert_called_once()
        assert (target / ".dep-version").read_text().strip() == "from-source-2025-01-31"
        # The pinned tag must appear in the download URL.
        assert "from-source-2025-01-31" in dl.call_args.args[0]

    def test_downloads_the_zip_the_engine_installer_names(self, tmp_path):
        # The engine's installer consumes godot-swappy.zip and lays the ABI
        # dirs out at the archive root; the 7z asset has a different shape.
        deps = tmp_path / "deps"
        up = self._upstream_with_swappy(tmp_path)

        with (
            mock.patch("scripts.host_orchestrator._download_with_retry") as dl,
            mock.patch(
                "scripts.host_orchestrator._extract",
                side_effect=lambda archive, dest: _stage_swappy_libs(dest),
            ),
        ):
            host_orchestrator._download_swappy(deps, up, dry_run=False)

        url, archive = dl.call_args.args[0], dl.call_args.args[1]
        assert url.endswith("/godot-swappy.zip")
        assert Path(archive).name == "godot-swappy.zip"

    def test_lays_out_arch_dirs_where_detect_swappy_probes(self, tmp_path):
        # apply_swappy copies this dir verbatim into
        # thirdparty/swappy-frame-pacing/, which detect_swappy() probes as
        # <arch>/libswappy_static.a — so the ABI dirs must be at the top level.
        deps = tmp_path / "deps"
        up = self._upstream_with_swappy(tmp_path)

        def fake_extract(archive, dest):
            _stage_swappy_libs(dest)
            (dest / "LICENSE").write_text("license")

        with (
            mock.patch("scripts.host_orchestrator._download_with_retry"),
            mock.patch("scripts.host_orchestrator._extract", side_effect=fake_extract),
        ):
            host_orchestrator._download_swappy(deps, up, dry_run=False)

        for arch in _SWAPPY_ARCHS:
            assert (deps / "swappy" / arch / "libswappy_static.a").is_file()

    def test_dry_run_leaves_an_existing_install_intact(self, tmp_path):
        # A dry run must not destroy a stale install it merely reports on.
        deps = tmp_path / "deps"
        target = deps / "swappy"
        _stage_swappy_libs(target, archs=("arm64-v8a",))
        up = self._upstream_with_swappy(tmp_path)

        with mock.patch("scripts.host_orchestrator._download_with_retry") as dl:
            host_orchestrator._download_swappy(deps, up, dry_run=True)

        dl.assert_not_called()
        assert (target / "arm64-v8a" / "libswappy_static.a").is_file()

    def test_raises_when_archive_lacks_an_arch_lib(self, tmp_path):
        # A restructured archive must fail the build loudly instead of
        # silently producing an Android build without frame pacing.
        deps = tmp_path / "deps"
        up = self._upstream_with_swappy(tmp_path)

        with (
            mock.patch("scripts.host_orchestrator._download_with_retry"),
            mock.patch(
                "scripts.host_orchestrator._extract",
                side_effect=lambda archive, dest: _stage_swappy_libs(
                    dest, archs=("arm64-v8a",)
                ),
            ),
            pytest.raises(host_orchestrator._HostOrchestratorError, match="x86_64"),
        ):
            host_orchestrator._download_swappy(deps, up, dry_run=False)


class TestEngineInstallStrList:
    """ABI lists are read from the engine too, so an added ABI upstream is
    picked up without editing this repo."""

    def _upstream(self, tmp_path, body):
        up = tmp_path / "upstream" / "godot"
        scripts = up / "misc" / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        (scripts / "install_swappy_android.py").write_text(body, encoding="utf-8")
        return up

    def test_parses_multiline_list_literal(self, tmp_path):
        up = self._upstream(
            tmp_path, _SWAPPY_INSTALL_SCRIPT.format(tag="from-source-2025-01-31")
        )

        assert host_orchestrator._engine_install_str_list(
            up, "install_swappy_android.py", "swappy_archs"
        ) == ["arm64-v8a", "armeabi-v7a", "x86", "x86_64"]

    def test_raises_when_list_is_absent(self, tmp_path):
        up = self._upstream(tmp_path, 'swappy_tag = "t"\n')

        with pytest.raises(host_orchestrator._HostOrchestratorError, match="archs"):
            host_orchestrator._engine_install_str_list(
                up, "install_swappy_android.py", "swappy_archs"
            )

    def test_raises_when_list_is_empty(self, tmp_path):
        up = self._upstream(tmp_path, "swappy_archs = []\n")

        with pytest.raises(host_orchestrator._HostOrchestratorError, match="archs"):
            host_orchestrator._engine_install_str_list(
                up, "install_swappy_android.py", "swappy_archs"
            )


class TestEngineInstallVersion:
    """The engine's install_*.py scripts are the single source of truth for
    dependency versions; we parse them rather than maintain a drifting copy."""

    def _script(self, tmp_path, name, body):
        up = tmp_path / "upstream" / "godot"
        scripts = up / "misc" / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        (scripts / name).write_text(body, encoding="utf-8")
        return up

    def test_parses_version_constant(self, tmp_path):
        up = self._script(
            tmp_path, "install_angle.py", '# c\nangle_version = "chromium/7219"\n'
        )
        assert (
            host_orchestrator._engine_install_version(
                up, "install_angle.py", "angle_version"
            )
            == "chromium/7219"
        )

    def test_raises_when_script_missing(self, tmp_path):
        up = tmp_path / "upstream" / "godot"
        up.mkdir(parents=True)
        with pytest.raises(host_orchestrator._HostOrchestratorError):
            host_orchestrator._engine_install_version(
                up, "install_angle.py", "angle_version"
            )

    def test_raises_when_var_missing(self, tmp_path):
        up = self._script(tmp_path, "install_angle.py", 'other = "x"\n')
        with pytest.raises(host_orchestrator._HostOrchestratorError):
            host_orchestrator._engine_install_version(
                up, "install_angle.py", "angle_version"
            )


class TestDownloadWinrt:
    def test_fetches_engine_pinned_version(self, tmp_path):
        deps = tmp_path / "deps"
        up = tmp_path / "upstream" / "godot"
        scripts = up / "misc" / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        (scripts / "install_winrt.py").write_text(
            'winrt_version = "72"\n', encoding="utf-8"
        )
        with (
            mock.patch("scripts.host_orchestrator._download_with_retry") as dl,
            mock.patch("scripts.host_orchestrator._extract"),
        ):
            host_orchestrator._download_winrt(deps, up, dry_run=False)
        dl.assert_called_once()
        assert "/72/winrt-headers.zip" in dl.call_args.args[0]
        assert (deps / "winrt" / ".dep-version").read_text().strip() == "72"


class TestVersionAwareDeps:
    """A version bump in the engine's pin must invalidate a stale local copy."""

    def _upstream(self, tmp_path, ac):
        up = tmp_path / "upstream" / "godot"
        scripts = up / "misc" / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        (scripts / "install_accesskit.py").write_text(
            f'ac_version = "{ac}"\n', encoding="utf-8"
        )
        return up

    def test_stale_version_triggers_refetch(self, tmp_path):
        deps = tmp_path / "deps"
        target = deps / "accesskit"
        (target / "accesskit-c").mkdir(parents=True)  # old copy present
        (target / ".dep-version").write_text("0.21.2")  # but stale marker
        up = self._upstream(tmp_path, ac="0.22.3")

        def fake_extract(archive, dest):
            (dest / "accesskit-c-0.22.3").mkdir(parents=True, exist_ok=True)

        with (
            mock.patch("scripts.host_orchestrator._download_with_retry") as dl,
            mock.patch("scripts.host_orchestrator._extract", side_effect=fake_extract),
        ):
            host_orchestrator._download_accesskit(deps, up, dry_run=False)
        dl.assert_called_once()
        assert "0.22.3/accesskit-c-0.22.3.zip" in dl.call_args.args[0]
        assert (target / ".dep-version").read_text().strip() == "0.22.3"

    def test_current_marker_skips_download(self, tmp_path):
        deps = tmp_path / "deps"
        target = deps / "accesskit"
        (target / "accesskit-c").mkdir(parents=True)
        (target / ".dep-version").write_text("0.22.3")
        up = self._upstream(tmp_path, ac="0.22.3")
        with mock.patch("scripts.host_orchestrator._download_with_retry") as dl:
            host_orchestrator._download_accesskit(deps, up, dry_run=False)
        dl.assert_not_called()


# ---------------------------------------------------------------------------
# _safe_extract rejects path-traversal entries
# ---------------------------------------------------------------------------


def _make_evil_zip(path: Path, member_name: str) -> None:
    """Write a zip containing a single entry called *member_name*.

    Used to construct an archive whose entry resolves outside the
    extraction directory once we ``extractall`` it.
    """
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(member_name, b"pwned")


def _make_evil_tar(path: Path, member_name: str) -> None:
    """Write a tar containing a single regular-file entry with payload bytes."""
    with tarfile.open(path, "w") as tf:
        data = b"pwned"
        info = tarfile.TarInfo(name=member_name)
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))


class TestSafeExtractRejectsPathTraversal:
    """Hardening test: a crafted archive entry that resolves outside
    the extraction destination must be refused with a clear error and must
    not write anything outside ``dest``."""

    def test_zip_relative_traversal_is_rejected(self, tmp_path):
        archive = tmp_path / "evil.zip"
        _make_evil_zip(archive, "../escaped.txt")
        dest = tmp_path / "extract"

        with pytest.raises(host_orchestrator._HostOrchestratorError) as exc:
            host_orchestrator._safe_extract(archive, dest)

        # Error message must mention the offending entry and the destination.
        assert "escaped.txt" in str(exc.value) or "../escaped.txt" in str(exc.value)
        # Nothing escaped: the parent dir of dest does not gain the file.
        assert not (tmp_path / "escaped.txt").exists()

    def test_zip_absolute_traversal_is_rejected(self, tmp_path):
        archive = tmp_path / "evil_abs.zip"
        # Absolute path inside the zip — extractall would otherwise write to /tmp.
        _make_evil_zip(archive, "/tmp/escaped_abs.txt")
        dest = tmp_path / "extract"

        with pytest.raises(host_orchestrator._HostOrchestratorError):
            host_orchestrator._safe_extract(archive, dest)

        assert not Path("/tmp/escaped_abs.txt").exists()

    def test_tar_relative_traversal_is_rejected(self, tmp_path):
        archive = tmp_path / "evil.tar"
        _make_evil_tar(archive, "../escaped_tar.txt")
        dest = tmp_path / "extract"

        with pytest.raises(
            (
                host_orchestrator._HostOrchestratorError,
                (
                    tarfile.OutsideDestinationError
                    if hasattr(tarfile, "OutsideDestinationError")
                    else Exception
                ),
            )
        ):
            host_orchestrator._safe_extract(archive, dest)

        assert not (tmp_path / "escaped_tar.txt").exists()

    def test_safe_zip_extracts_cleanly(self, tmp_path):
        # Sanity: a benign entry still extracts successfully via _safe_extract.
        archive = tmp_path / "good.zip"
        _make_evil_zip(archive, "subdir/file.txt")
        dest = tmp_path / "extract"

        host_orchestrator._safe_extract(archive, dest)

        assert (dest / "subdir" / "file.txt").is_file()

    def test_safe_tar_extracts_cleanly(self, tmp_path):
        archive = tmp_path / "good.tar"
        _make_evil_tar(archive, "subdir/file.txt")
        dest = tmp_path / "extract"

        host_orchestrator._safe_extract(archive, dest)

        assert (dest / "subdir" / "file.txt").is_file()


# ---------------------------------------------------------------------------
# build flag derivation
# ---------------------------------------------------------------------------


class TestResolveBuildFlags:
    def test_all_enables_both(self):
        classical, mono = host_orchestrator._resolve_build_flags("all")
        assert classical is True and mono is True

    def test_classical_enables_classical_only(self):
        classical, mono = host_orchestrator._resolve_build_flags("classical")
        assert classical is True and mono is False

    def test_mono_enables_mono_only(self):
        classical, mono = host_orchestrator._resolve_build_flags("mono")
        assert classical is False and mono is True

    def test_unknown_raises(self):
        with pytest.raises(host_orchestrator._HostOrchestratorError):
            host_orchestrator._resolve_build_flags("nonsense")


# ---------------------------------------------------------------------------
# Chown
# ---------------------------------------------------------------------------


class TestChown:
    def test_permission_error_is_non_fatal(self, tmp_path, caplog):
        # Stage real targets so the chown loop tries them.
        (tmp_path / "out").mkdir()
        (tmp_path / "mono-glue").mkdir()
        (tmp_path / "godot-4.8.tar.gz").write_text("tarball")

        caplog.set_level(logging.INFO, logger="scripts.host_orchestrator")
        with mock.patch(
            "scripts.host_orchestrator.os.chown", side_effect=PermissionError("nope")
        ):
            # Must NOT raise.
            host_orchestrator._chown_outputs(tmp_path, dry_run=False)

        assert "non-fatal" in caplog.text.lower()

    def test_dry_run_does_not_chown(self, tmp_path):
        with mock.patch("scripts.host_orchestrator.os.chown") as chown:
            host_orchestrator._chown_outputs(tmp_path, dry_run=True)
        chown.assert_not_called()

    def test_chown_continues_on_per_target_failure(self, tmp_path):
        # A PermissionError on one chown target must NOT
        # short-circuit the loop. Every existing target should still be
        # attempted so a partial-permission environment doesn't silently
        # leave most outputs unchanged.
        (tmp_path / "out").mkdir()
        (tmp_path / "mono-glue").mkdir()
        (tmp_path / "godot-4.8.tar.gz").write_text("tarball")

        # First chown call raises; subsequent ones succeed. We assert the
        # loop reaches all three top-level targets even after the first
        # error. (``_chown_recursive`` calls os.chown on each target then
        # on its children — but the path-level chown for each top-level
        # target is enough to prove the loop kept going.)
        attempted_targets: list[Path] = []

        def fake_chown(path, uid, gid):
            attempted_targets.append(Path(path))
            # Only the *first* top-level target raises; the rest succeed.
            if Path(path) == tmp_path / "out":
                raise PermissionError("simulated container uid mismatch")

        with mock.patch("scripts.host_orchestrator.os.chown", side_effect=fake_chown):
            host_orchestrator._chown_outputs(tmp_path, dry_run=False)

        # All three top-level targets must have been attempted.
        assert tmp_path / "out" in attempted_targets
        assert tmp_path / "mono-glue" in attempted_targets
        assert tmp_path / "godot-4.8.tar.gz" in attempted_targets


# ---------------------------------------------------------------------------
# run_build — end-to-end with everything mocked
# ---------------------------------------------------------------------------


class TestRunBuildEndToEnd:
    """End-to-end exercise of :func:`run_build` with every external call
    mocked. We assert that the orchestrator wires through the right steps and
    honours skip-flags/gates."""

    def _stage_basedir(self, tmp_path: Path) -> tuple[Path, Path]:
        basedir = tmp_path / "build-godot-and-templates"
        basedir.mkdir()
        # Pre-stage the tarball + version.py so _prepare_source can run.
        upstream = tmp_path / "upstream" / "godot"
        _make_version_py(upstream)
        return basedir, upstream

    def _common_patches(self):
        """Return a stack of patches that no-op out every external call."""
        # Use ExitStack pattern via individual context managers in tests.
        ...

    def test_dry_run_walks_full_flow_without_docker_or_curl(self, tmp_path):
        basedir, upstream = self._stage_basedir(tmp_path)

        # ``_prepare_source`` still needs `git rev-parse` even in dry-run to
        # report the current ref; mock it but leave docker/Popen/urlretrieve
        # absolutely untouched.
        def fake_run(cmd, *args, **kwargs):
            if "rev-parse" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="4.8.dev1\n", stderr=""
                )
            if _is_version_read_subprocess(cmd):
                return _real_version_read(cmd)
            raise AssertionError(f"Dry run should not subprocess for: {cmd}")

        with (
            mock.patch(
                "scripts.host_orchestrator.subprocess.run", side_effect=fake_run
            ),
            mock.patch("scripts.host_orchestrator.subprocess.Popen") as popen,
            mock.patch(
                "scripts.host_orchestrator.urllib.request.urlretrieve"
            ) as urlretrieve,
        ):
            rc = host_orchestrator.run_build(
                basedir=basedir,
                registry="ghcr.io",
                username="u",
                container_version="4.8",
                git_branch="4.8.dev1",
                godot_repo="u/godot",
                upstream_godot_dir=upstream,
                build_type="all",
                num_cores=4,
                dry_run=True,
            )

        assert rc == 0
        # Dry run never invokes docker / curl / popen.
        popen.assert_not_called()
        urlretrieve.assert_not_called()

    def test_iter_platforms_always_yields_apple_targets(self):
        # Apple targets always attempt to build — _iter_platforms must yield
        # macOS and iOS unconditionally; failures surface inside the container.
        images = {
            plat: f"ghcr.io/u/godot-{plat}:4.8"
            for plat in ("linux", "android", "windows", "macos", "ios", "web")
        }
        yielded = [
            name
            for name, _img, _mounts, _env in host_orchestrator._iter_platforms(
                basedir=Path("/tmp/bd"), images=images
            )
        ]
        assert "macos" in yielded
        assert "ios" in yielded

    def test_resumability_skips_populated_platform_dirs(self, tmp_path):
        basedir, upstream = self._stage_basedir(tmp_path)

        # Populate every platform AND mono-glue so the entire build flow
        # short-circuits.
        for plat in ("linux", "windows", "android", "macos", "ios", "web"):
            _populate_platform_output(basedir / "out" / plat)
        (basedir / "mono-glue" / "GodotSharp").mkdir(parents=True)
        (basedir / "mono-glue" / "GodotSharp" / "x.cs").write_text("x")

        def fake_run(cmd, *args, **kwargs):
            if "rev-parse" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="4.8.dev1\n", stderr=""
                )
            if _is_version_read_subprocess(cmd):
                return _real_version_read(cmd)
            if cmd[:3] == ["docker", "image", "inspect"]:
                return subprocess.CompletedProcess(cmd, 1)
            if cmd[:2] == ["docker", "pull"]:
                return subprocess.CompletedProcess(cmd, 0)
            if cmd[0] == "sh":
                (upstream.parent / "godot-4.8.tar.gz").write_text("tarball")
                return subprocess.CompletedProcess(cmd, 0)
            raise AssertionError(f"Unexpected subprocess call: {cmd}")

        with (
            mock.patch(
                "scripts.host_orchestrator.subprocess.run", side_effect=fake_run
            ),
            mock.patch("scripts.host_orchestrator.subprocess.Popen") as popen,
            mock.patch("scripts.host_orchestrator._download_deps"),
        ):
            rc = host_orchestrator.run_build(
                basedir=basedir,
                registry="ghcr.io",
                username="u",
                container_version="4.8",
                git_branch="4.8.dev1",
                godot_repo="u/godot",
                upstream_godot_dir=upstream,
                build_type="all",
                num_cores=4,
                dry_run=False,
            )

        assert rc == 0
        # Every platform was skipped, so no docker run.
        popen.assert_not_called()

    def test_run_build_does_not_chown(self, tmp_path):
        # The cosmetic chown moved out of run_build to the tail of the release
        # flow (after publishing) so an interruption during it can't cost the
        # build. run_build itself must therefore never call os.chown.
        basedir, upstream = self._stage_basedir(tmp_path)
        for plat in ("linux", "windows", "android", "macos", "ios", "web"):
            _populate_platform_output(basedir / "out" / plat)
        (basedir / "mono-glue" / "GodotSharp").mkdir(parents=True)
        (basedir / "mono-glue" / "GodotSharp" / "x.cs").write_text("x")

        def fake_run(cmd, *args, **kwargs):
            if "rev-parse" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="4.8.dev1\n", stderr=""
                )
            if _is_version_read_subprocess(cmd):
                return _real_version_read(cmd)
            if cmd[:3] == ["docker", "image", "inspect"]:
                return subprocess.CompletedProcess(cmd, 1)
            if cmd[:2] == ["docker", "pull"]:
                return subprocess.CompletedProcess(cmd, 0)
            if cmd[0] == "sh":
                (upstream.parent / "godot-4.8.tar.gz").write_text("tarball")
                return subprocess.CompletedProcess(cmd, 0)
            raise AssertionError(f"Unexpected: {cmd}")

        with (
            mock.patch(
                "scripts.host_orchestrator.subprocess.run", side_effect=fake_run
            ),
            mock.patch("scripts.host_orchestrator._download_deps"),
            mock.patch("scripts.host_orchestrator.os.chown") as chown,
        ):
            rc = host_orchestrator.run_build(
                basedir=basedir,
                registry="ghcr.io",
                username="u",
                container_version="4.8",
                git_branch="4.8.dev1",
                godot_repo="u/godot",
                upstream_godot_dir=upstream,
                build_type="all",
                num_cores=4,
                dry_run=False,
            )

        assert rc == 0
        chown.assert_not_called()


# ---------------------------------------------------------------------------
# Clean modes
# ---------------------------------------------------------------------------


class TestCleanModes:
    def test_clean_release_removes_build_outputs_but_keeps_deps(self, tmp_path):
        for name in ("mono-glue", "out", "releases", "deps"):
            (tmp_path / name).mkdir()
            (tmp_path / name / "marker").write_text("x")
        (tmp_path / "godot-4.8.tar.gz").write_text("tarball")

        rc = host_orchestrator.run_build(
            basedir=tmp_path,
            registry="ghcr.io",
            username="u",
            container_version="4.8",
            git_branch="4.8.dev1",
            godot_repo="u/godot",
            upstream_godot_dir=tmp_path / "irrelevant",
            build_type="all",
            num_cores=4,
            mode="clean-release",
        )

        assert rc == 0
        assert not (tmp_path / "mono-glue").exists()
        assert not (tmp_path / "out").exists()
        assert not (tmp_path / "releases").exists()
        assert not (tmp_path / "godot-4.8.tar.gz").exists()
        # deps/ is preserved.
        assert (tmp_path / "deps").is_dir()

    def test_cleanup_removes_deps_too(self, tmp_path):
        for name in ("mono-glue", "out", "deps"):
            (tmp_path / name).mkdir()
            (tmp_path / name / "marker").write_text("x")

        rc = host_orchestrator.run_build(
            basedir=tmp_path,
            registry="ghcr.io",
            username="u",
            container_version="4.8",
            git_branch="4.8.dev1",
            godot_repo="u/godot",
            upstream_godot_dir=tmp_path / "irrelevant",
            build_type="all",
            num_cores=4,
            mode="cleanup",
        )

        assert rc == 0
        assert not (tmp_path / "deps").exists()


# ---------------------------------------------------------------------------
# Docker invocation contract: must call `python3 -m scripts.in_container.*`
# with the host's scripts/ dir mounted at /root/build-scripts/scripts (read-only)
# and PYTHONPATH pointing at the PARENT /root/build-scripts so that `scripts`
# resolves as a top-level package. No legacy ``bash build/build.sh`` may appear.
# ---------------------------------------------------------------------------


class TestPhase3DockerInvocation:
    def test_common_docker_args_mount_scripts_and_set_pythonpath(self, tmp_path):
        tarball = tmp_path / "godot-4.8.tar.gz"
        tarball.write_text("t")
        mono_glue = tmp_path / "mono-glue"
        mono_glue.mkdir()

        args = host_orchestrator._common_docker_args(
            tarball=tarball,
            mono_glue_dir=mono_glue,
            env={"CLASSICAL": "1"},
        )

        # PYTHONPATH must be the PARENT of the mount target so that the
        # top-level ``scripts`` package name in ``python3 -m
        # scripts.in_container.X`` resolves.
        assert any(
            a == f"PYTHONPATH={host_orchestrator._SCRIPTS_PYTHONPATH}" for a in args
        )
        # The PYTHONPATH must be the parent dir of the mount target.
        assert (
            host_orchestrator._SCRIPTS_MOUNT_TARGET
            == f"{host_orchestrator._SCRIPTS_PYTHONPATH}/scripts"
        )
        # scripts/ mount must appear as -v <host>:<container>:ro at the
        # nested mount target (so the container sees /root/build-scripts/scripts).
        mount_target = host_orchestrator._SCRIPTS_MOUNT_TARGET
        mount_args = [a for a in args if mount_target in a]
        assert any(":ro" in a and a.endswith(f":{mount_target}:ro") for a in mount_args)

    def test_mono_glue_invokes_python3_module(self, tmp_path):
        basedir = tmp_path / "build-godot-and-templates"
        basedir.mkdir()
        (basedir / "build-mono-glue").mkdir()
        (basedir / "logs" / "20260101-120000").mkdir(parents=True)
        run_logs_dir = basedir / "logs" / "20260101-120000"
        tarball = basedir / "godot-4.8.tar.gz"
        tarball.write_text("t")
        mono_glue = basedir / "mono-glue"
        mono_glue.mkdir()

        captured: dict = {}

        def fake_tee(cmd, *, log_path, dry_run):
            captured["cmd"] = cmd

        with mock.patch("scripts.host_orchestrator._run_and_tee", side_effect=fake_tee):
            host_orchestrator._run_build_mono_glue(
                basedir=basedir,
                linux_image="ghcr.io/u/godot-linux:4.8",
                tarball=tarball,
                mono_glue_dir=mono_glue,
                run_logs_dir=run_logs_dir,
                env={"CLASSICAL": "1", "MONO": "1"},
                dry_run=False,
            )

        cmd = captured["cmd"]
        # The last three args invoke the in-container Python module.
        assert cmd[-3:] == ["python3", "-m", "scripts.in_container.build_mono_glue"]
        # No `bash build/build.sh` anywhere.
        assert "bash" not in cmd
        assert "build/build.sh" not in cmd

    def test_run_build_platform_invokes_python3_per_platform_module(self, tmp_path):
        basedir = tmp_path / "build-godot-and-templates"
        basedir.mkdir()
        tarball = basedir / "godot-4.8.tar.gz"
        tarball.write_text("t")
        mono_glue = basedir / "mono-glue"
        mono_glue.mkdir()
        run_logs_dir = basedir / "logs" / "20260101-120000"
        run_logs_dir.mkdir(parents=True)

        captured: dict = {}

        def fake_tee(cmd, *, log_path, dry_run):
            captured["cmd"] = cmd

        with mock.patch("scripts.host_orchestrator._run_and_tee", side_effect=fake_tee):
            host_orchestrator._run_build_platform(
                name="linux",
                image="ghcr.io/u/godot-linux:4.8",
                basedir=basedir,
                tarball=tarball,
                mono_glue_dir=mono_glue,
                run_logs_dir=run_logs_dir,
                out_plat=basedir / "out" / "linux",
                env={"CLASSICAL": "1"},
                extra_mounts=["-v", f"{basedir}/build-linux:/root/build"],
                dry_run=False,
            )

        cmd = captured["cmd"]
        assert cmd[-3:] == ["python3", "-m", "scripts.in_container.build_linux"]

    def test_run_build_platform_dispatches_per_platform_module_name(self, tmp_path):
        basedir = tmp_path / "b"
        basedir.mkdir()
        tarball = basedir / "godot-4.8.tar.gz"
        tarball.write_text("t")
        mono_glue = basedir / "mg"
        mono_glue.mkdir()
        run_logs_dir = basedir / "logs" / "20260101-120000"
        run_logs_dir.mkdir(parents=True)

        for plat in ("windows", "android", "macos", "ios", "web"):
            captured: dict = {}

            def fake_tee(cmd, *, log_path, dry_run, _captured=captured):
                _captured["cmd"] = cmd

            with mock.patch(
                "scripts.host_orchestrator._run_and_tee", side_effect=fake_tee
            ):
                host_orchestrator._run_build_platform(
                    name=plat,
                    image="img",
                    basedir=basedir,
                    tarball=tarball,
                    mono_glue_dir=mono_glue,
                    out_plat=basedir / plat,
                    run_logs_dir=run_logs_dir,
                    env={},
                    extra_mounts=[],
                    dry_run=False,
                )
            assert captured["cmd"][-3:] == [
                "python3",
                "-m",
                f"scripts.in_container.build_{plat}",
            ]
