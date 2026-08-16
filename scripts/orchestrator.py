"""Release orchestration: drive the host orchestrator, run the Python
packager, and publish.

This module owns:

  - driving :func:`scripts.host_orchestrator.run_build` via :func:`dispatch_build`
    to build the matrix (Mono glue + per-platform passes),
  - calling :func:`scripts.packager.package_release` via :func:`package_release`
    to stage editor zips, .tpz bundles and SHA512-SUMS.txt files,
  - publishing a real GitHub Release via ``gh``.

All subprocess and ``gh`` calls are funnelled through small, individually
patchable functions so tests can exercise the wiring without Docker, the
network, or a real ``gh``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import zipfile
from collections import Counter
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree

from scripts import host_orchestrator, packager

logger = logging.getLogger(__name__)


class PublishError(Exception):
    """Raised when a publish step fails (maps to exit code 5).

    Covers both the ``gh release`` upload and the GitHub Packages NuGet push.
    """


# Environment variables consulted (in order) for the GitHub Packages token.
# GITHUB_PERSONAL_ACCESS_TOKEN is the operator's configured PAT var; GHCR_PAT is
# the repo's image-push convention (same write:packages scope works for the
# NuGet registry); the CI-injected GITHUB_TOKEN is the final fallback.
_NUGET_TOKEN_ENV_VARS = ("GITHUB_PERSONAL_ACCESS_TOKEN", "GHCR_PAT", "GITHUB_TOKEN")

# The four managed NuGet packages Godot's Mono build emits are platform- and
# arch-invariant, so the same package id+version is produced under every
# ``out/<plat>/<arch>/tools-mono/GodotSharp/Tools/nupkgs/`` dir. We publish one
# canonical copy; this is the preferred source (Linux x86_64 always builds
# first). ``.snupkg`` symbol packages are intentionally excluded — GitHub
# Packages has no symbol server, so `dotnet nuget push --no-symbols` is used.
_NUGET_CANONICAL_PREFERENCE = ("linux", "x86_64")


# ---------------------------------------------------------------------------
# Build + packaging (all-Python pipeline)
# ---------------------------------------------------------------------------


def dispatch_build(
    *,
    build_dir: Path,
    build_type: str,
    num_cores: int,
    git_branch: str,
    godot_repo: str,
    registry: str,
    username: str,
    container_version: str,
    platforms: list[str] | None = None,
    build_archs: list[str] | None = None,
    force: bool = False,
    dry_run: bool = False,
    run_logs_dir: Path | None = None,
) -> int:
    """Drive the host orchestrator to build the matrix in-process.

    *force* rebuilds unconditionally: the per-platform ``out/`` and the Mono
    glue skip guards are bypassed and their stale artifacts cleared first, so
    a source change is guaranteed to be recompiled rather than re-packaged.

    Delegates to :func:`scripts.host_orchestrator.run_build` — generate the
    Mono glue once, acquire images, run per-platform docker passes with the
    resumability gate. *platforms* (``None`` = all six) scopes the run to a
    subset; *build_archs* (``None`` = all) restricts the in-container arch
    matrix. Within the scoped set, Apple targets (macos/ios) are always
    attempted; the in-container build emits a clear error if its toolchain is
    not set up.

    *container_version* is the Godot version (e.g. ``"4.8"``) and resolves
    image refs as ``{registry}/{username}/godot-<plat>:{container_version}``
    — matching what ``containers --push`` produces and what ``config.toml``
    declares. It must be threaded here because the build resolves images
    *before* it extracts the engine version, so we cannot derive the tag
    from the source tree.

    The Apple SDK *version* strings (``XCODE_SDKV`` / ``APPLE_SDKV``) are NOT
    threaded here: they are config's single source of truth at image-build
    time, baked into the Apple images via ``container_builder``
    ``--build-arg``. The host orchestrator never consumes them at run time.

    Returns the exit code (0 = success).
    """
    upstream_godot_dir = (build_dir.parent / "upstream" / "godot").resolve()
    return host_orchestrator.run_build(
        basedir=build_dir,
        registry=registry,
        username=username,
        container_version=container_version,
        git_branch=git_branch,
        godot_repo=godot_repo,
        upstream_godot_dir=upstream_godot_dir,
        build_type=build_type,
        num_cores=num_cores,
        platforms=platforms,
        build_archs=build_archs,
        force=force,
        dry_run=dry_run,
        run_logs_dir=run_logs_dir,
    )


def package_release(
    *,
    build_dir: Path,
    godot_version: str,
    godot_version_status: str,
    upstream_godot_dir: Path | None = None,
    dry_run: bool = False,
) -> int:
    """Run the host-side packaging step.

    Calls :func:`scripts.packager.package_release` in-process: emit editor
    zips, classical + Mono ``.tpz`` export-template bundles (each with
    ``version.txt``), and ``SHA512-SUMS.txt``. Publishing is owned by
    :func:`publish_release`.

    Parameters
    ----------
    build_dir
        ``build-godot-and-templates/`` — the dir holding ``out/`` /
        ``releases/`` / ``deps/`` etc.
    godot_version
        Engine version (``major.minor[.patch]``), e.g. ``"4.8"``.
    godot_version_status
        Engine pre-release status from ``upstream/godot/version.py``, e.g.
        ``"beta"``. This is the SINGLE source of truth: it drives both the
        published filename pattern (``Godot_v<godot_version>.<status>_*``)
        AND ``version.txt`` inside the ``.tpz``. Godot looks templates up
        by ``version.txt`` at install time, so this MUST match what the
        engine binary reports at runtime.
    upstream_godot_dir
        Optional override for the path to the checked-out Godot source. When
        ``None`` (the default), the packager picks
        ``<build_dir>.parent/upstream/godot`` — the path used by the rest of
        ``build-godot.py``.
    dry_run
        Log actions without writing anything.

    Returns ``0`` on success; non-zero on a hard packaging error.
    """
    if dry_run:
        logger.info(
            "[dry-run] Would run packager.package_release(basedir=%s, "
            "godot_version=%s, godot_version_status=%s, upstream_godot_dir=%s).",
            build_dir,
            godot_version,
            godot_version_status,
            upstream_godot_dir,
        )
        return 0
    return packager.package_release(
        basedir=build_dir,
        godot_version=godot_version,
        godot_version_status=godot_version_status,
        upstream_godot_dir=upstream_godot_dir,
        dry_run=False,
    )


def collect_release_assets(release_dir: Path) -> list[Path]:
    """Return every publishable asset under *release_dir* (recursively).

    Includes editor zips, ``.tpz`` bundles, and ``SHA512-SUMS.txt`` from both
    the classical dir and the ``mono/`` subdir.
    """
    if not release_dir.is_dir():
        return []
    return sorted(p for p in release_dir.rglob("*") if p.is_file())


# ---------------------------------------------------------------------------
# Publishing (gh release)
# ---------------------------------------------------------------------------


def _gh_available() -> bool:
    return shutil.which("gh") is not None


def _release_exists(tag: str, repo: str, dry_run: bool = False) -> bool:
    """Return ``True`` if a GitHub Release already exists for *tag*."""
    if dry_run:
        # In dry-run we cannot query; assume it must be created (upload would
        # otherwise need --clobber). The dry-run command print below shows both.
        return False
    result = subprocess.run(
        ["gh", "release", "view", tag, "--repo", repo],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def build_create_command(
    *,
    tag: str,
    repo: str,
    assets: list[str],
    prerelease: bool,
    draft: bool,
) -> list[str]:
    """Construct the ``gh release create`` command (no execution).

    Pure helper so tests can assert the exact command without invoking ``gh``.
    Used only when no Release exists yet for *tag*; the override path
    (:func:`clear_release_assets` + ``gh release upload``) handles an existing
    Release.
    """
    cmd = ["gh", "release", "create", tag, *assets, "--repo", repo]
    if prerelease:
        cmd.append("--prerelease")
    if draft:
        cmd.append("--draft")
    # Reuse the existing tag without re-tagging or generating extra notes.
    cmd.extend(["--title", tag, "--notes", f"Custom Godot build {tag}."])
    return cmd


def build_list_release_assets_command(*, tag: str, repo: str) -> list[str]:
    """Construct the ``gh`` command that lists an existing Release's asset names."""
    return [
        "gh",
        "release",
        "view",
        tag,
        "--repo",
        repo,
        "--json",
        "assets",
        "--jq",
        ".assets[].name",
    ]


def build_delete_release_asset_command(
    *, tag: str, repo: str, asset_name: str
) -> list[str]:
    """Construct the ``gh release delete-asset`` command for one asset."""
    return ["gh", "release", "delete-asset", tag, asset_name, "--repo", repo, "--yes"]


def clear_release_assets(*, tag: str, repo: str, dry_run: bool = False) -> None:
    """Delete every asset currently attached to the Release on *tag*.

    This is the override primitive: wiping the existing assets before a fresh
    upload yields a clean slate, which sidesteps ``gh release upload --clobber``
    racing on a stale asset id (it deletes-by-id, and a half-finished prior run
    can leave the id gone — a 404 that aborts the whole upload). A missing
    Release is treated as "nothing to clear".

    Raises
    ------
    PublishError
        If listing succeeds but an individual asset deletion fails.
    """
    list_cmd = build_list_release_assets_command(tag=tag, repo=repo)
    if dry_run:
        logger.info("[dry-run] Would clear existing assets via: %s", " ".join(list_cmd))
        return

    result = subprocess.run(list_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        # No Release (or no read access) -> nothing to clear; the caller's
        # create path will make it.
        return

    names = [n.strip() for n in result.stdout.splitlines() if n.strip()]
    if not names:
        return
    logger.info("Clearing %d existing asset(s) from Release '%s'.", len(names), tag)
    for name in names:
        del_cmd = build_delete_release_asset_command(
            tag=tag, repo=repo, asset_name=name
        )
        del_result = subprocess.run(del_cmd, capture_output=True, text=True)
        if del_result.returncode != 0:
            raise PublishError(
                f"Failed to delete existing asset '{name}' from Release "
                f"'{tag}': {del_result.stderr.strip()}"
            )


def disambiguate_asset_names(assets: list[Path]) -> dict[Path, str]:
    """Map each asset path to a unique upload name, resolving basename clashes.

    GitHub Release assets share a single flat namespace, but the packager stages
    a ``SHA512-SUMS.txt`` in both the classical release dir and the ``mono/``
    subdir — identical basenames that would collide (the second silently
    overwrites the first). When two staged files share a basename, the one(s)
    living in a subdirectory are suffixed with that subdir (e.g.
    ``mono/SHA512-SUMS.txt`` -> ``SHA512-SUMS-mono.txt``), matching the naming the
    classical/mono split has used historically. Files with a unique basename
    keep it.
    """
    if not assets:
        return {}
    if len(assets) == 1:
        return {assets[0]: assets[0].name}

    root = Path(os.path.commonpath([str(a) for a in assets]))
    counts = Counter(a.name for a in assets)
    resolved: dict[Path, str] = {}
    for asset in assets:
        if counts[asset.name] > 1:
            rel_parent = asset.relative_to(root).parent
            if rel_parent != Path("."):
                suffix = rel_parent.as_posix().replace("/", "-")
                resolved[asset] = f"{asset.stem}-{suffix}{asset.suffix}"
                continue
        resolved[asset] = asset.name
    return resolved


def publish_release(
    *,
    tag: str,
    repo: str,
    assets: list[Path],
    prerelease: bool,
    draft: bool,
    dry_run: bool = False,
) -> None:
    """Publish *assets* to the GitHub Release on *tag*, overriding any existing one.

    When no Release exists for the tag, ``gh release create`` makes it. When one
    exists, its assets are cleared (:func:`clear_release_assets`) and the freshly
    built assets are uploaded to the clean slate — a deterministic override that
    avoids the ``--clobber`` stale-id race. Basename collisions across the
    classical/``mono`` dirs are resolved via :func:`disambiguate_asset_names` and
    materialised as symlinks in a temp dir so ``gh`` uploads them under unique
    names without copying multi-GB files.

    Raises
    ------
    PublishError
        If ``gh`` is unavailable, there are no assets, or the publish exits
        non-zero. The caller maps this to exit code 5.
    """
    if not dry_run and not _gh_available():
        raise PublishError(
            "gh CLI not found on PATH. Install GitHub CLI and run "
            "`gh auth login` before publishing a Release."
        )
    if not assets:
        raise PublishError(
            f"No release assets found to publish for tag '{tag}'. "
            "Run the build + package steps first."
        )

    exists = _release_exists(tag, repo, dry_run=dry_run)
    upload_names = disambiguate_asset_names(assets)

    if exists:
        # Override: wipe existing assets first so the upload is a clean slate.
        clear_release_assets(tag=tag, repo=repo, dry_run=dry_run)

    if dry_run:
        names = sorted(upload_names.values())
        if exists:
            upload_cmd = ["gh", "release", "upload", tag, *names, "--repo", repo]
            logger.info(
                "[dry-run] Would clear existing assets, then run: %s",
                " ".join(upload_cmd),
            )
        else:
            create_cmd = build_create_command(
                tag=tag, repo=repo, assets=names, prerelease=prerelease, draft=draft
            )
            logger.info("[dry-run] Would run: %s", " ".join(create_cmd))
        return

    logger.info("Publishing %d asset(s) to Release '%s' on %s", len(assets), tag, repo)
    # Stage symlinks named by their resolved upload name; gh uploads using the
    # link's basename and reads content through it, so colliding files land
    # under distinct asset names without duplicating bytes.
    with tempfile.TemporaryDirectory(prefix="godot-release-assets-") as staging:
        staging_dir = Path(staging)
        staged: list[str] = []
        for path, name in upload_names.items():
            link = staging_dir / name
            try:
                link.symlink_to(path.resolve())
            except OSError:
                shutil.copy2(path, link)
            staged.append(str(link))
        staged.sort()

        if exists:
            cmd = ["gh", "release", "upload", tag, *staged, "--repo", repo]
        else:
            cmd = build_create_command(
                tag=tag,
                repo=repo,
                assets=staged,
                prerelease=prerelease,
                draft=draft,
            )
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as exc:
            raise PublishError(
                f"`gh release` failed with exit code {exc.returncode} for tag '{tag}'."
            ) from exc


# ---------------------------------------------------------------------------
# Publishing (GitHub Packages NuGet)
# ---------------------------------------------------------------------------


def default_nuget_source(username: str) -> str:
    """Return the GitHub Packages NuGet feed URL for *username*.

    GitHub Packages exposes one NuGet feed per account/org at
    ``https://nuget.pkg.github.com/<owner>/index.json``. The packages land in
    that owner's namespace (e.g. ``.../nongvantinh/pkgs/nuget/GodotSharp``).
    """
    return f"https://nuget.pkg.github.com/{username}/index.json"


def _dotnet_available() -> bool:
    return shutil.which("dotnet") is not None


def nuget_token_from_env() -> str | None:
    """Return the first non-empty GitHub Packages token from the environment.

    Consults :data:`_NUGET_TOKEN_ENV_VARS` in order. The token needs the
    ``write:packages`` scope — the very same scope ``GHCR_PAT`` already carries
    for image pushes, so no new credential is required for the common case.
    """
    for var in _NUGET_TOKEN_ENV_VARS:
        value = os.environ.get(var)
        if value:
            return value
    return None


def collect_nupkgs(out_dir: Path) -> list[Path]:
    """Return one canonical ``.nupkg`` per package id+version under *out_dir*.

    Godot's Mono build writes the managed packages to every
    ``out/<plat>/<arch>/tools-mono/GodotSharp/Tools/nupkgs/`` dir, so the same
    filename appears many times. GitHub Packages rejects re-publishing an
    existing id+version, and the copies are functionally identical, so we
    deduplicate by filename and return a single copy each — preferring the
    Linux x86_64 build (:data:`_NUGET_CANONICAL_PREFERENCE`) for determinism.

    ``.snupkg`` symbol packages are excluded (GitHub Packages has no symbol
    server). Returns an empty list when no nupkgs are present (a classical-only
    build), which the caller treats as "nothing to publish", not an error.
    """
    if not out_dir.is_dir():
        return []

    candidates = sorted(
        p
        for p in out_dir.rglob("tools-mono/GodotSharp/Tools/nupkgs/*.nupkg")
        if p.is_file()
    )

    def _is_preferred(path: Path) -> bool:
        parts = set(path.parts)
        return all(token in parts for token in _NUGET_CANONICAL_PREFERENCE)

    chosen: dict[str, Path] = {}
    for path in candidates:
        existing = chosen.get(path.name)
        # First match wins, but a preferred-source match always overrides a
        # non-preferred one already recorded for the same filename.
        if existing is None or (_is_preferred(path) and not _is_preferred(existing)):
            chosen[path.name] = path
    return [chosen[name] for name in sorted(chosen)]


def read_nupkg_identity(nupkg: Path) -> tuple[str, str]:
    """Return the ``(id, version)`` declared inside *nupkg*'s ``.nuspec``.

    A ``.nupkg`` is a zip archive containing exactly one ``<id>.nuspec`` at its
    root. Both the package id (``Godot.NET.Sdk``) and the version
    (``4.8.0-beta``) contain dots, so parsing them out of the *filename* is
    ambiguous; reading the nuspec is unambiguous. The nuspec uses a default XML
    namespace, so tags are matched by local name.

    Raises
    ------
    PublishError
        If the archive has no ``.nuspec`` or it lacks ``id``/``version``.
    """
    with zipfile.ZipFile(nupkg) as archive:
        nuspec_name = next(
            (n for n in archive.namelist() if n.lower().endswith(".nuspec")), None
        )
        if nuspec_name is None:
            raise PublishError(f"No .nuspec found inside '{nupkg.name}'.")
        with archive.open(nuspec_name) as handle:
            root = ElementTree.parse(handle).getroot()

    def _local(tag: str) -> str | None:
        for element in root.iter():
            if element.tag.rsplit("}", 1)[-1] == tag:
                return (element.text or "").strip() or None
        return None

    pkg_id = _local("id")
    version = _local("version")
    if not pkg_id or not version:
        raise PublishError(
            f"Could not read <id>/<version> from the nuspec in '{nupkg.name}'."
        )
    return pkg_id, version


def build_nuget_list_versions_command(*, username: str, package_name: str) -> list[str]:
    """Construct the ``gh api`` command that lists a NuGet package's versions.

    Pure helper so tests can assert the exact command. ``--paginate`` merges all
    pages into a single JSON array. The package name is URL-encoded because ids
    such as ``Godot.NET.Sdk`` are passed verbatim in the path.
    """
    return [
        "gh",
        "api",
        "--paginate",
        f"/users/{username}/packages/nuget/{quote(package_name, safe='')}/versions",
    ]


def build_nuget_delete_version_command(
    *, username: str, package_name: str, version_id: int
) -> list[str]:
    """Construct the ``gh api`` command that deletes one NuGet package version."""
    return [
        "gh",
        "api",
        "-X",
        "DELETE",
        f"/users/{username}/packages/nuget/{quote(package_name, safe='')}"
        f"/versions/{version_id}",
    ]


def _gh_env(api_key: str) -> dict[str, str]:
    """Return an environment for ``gh api`` that forces *api_key* as the token.

    The operator's ``gh auth login`` token only carries ``repo``/``workflow``
    scopes — enough to publish a Release, but not to delete a package version
    (that needs ``delete:packages``). The NuGet PAT does carry it, so we hand it
    to ``gh`` via ``GH_TOKEN`` (which ``gh`` prefers over the keyring) for the
    delete calls only.
    """
    env = dict(os.environ)
    if api_key:
        env["GH_TOKEN"] = api_key
    return env


def delete_nupkg_versions(
    *,
    nupkgs: list[Path],
    username: str,
    api_key: str,
    dry_run: bool = False,
) -> None:
    """Delete any already-published GitHub Packages version matching *nupkgs*.

    For each package, reads its ``(id, version)`` from the nuspec, lists the
    versions published under *username*'s NuGet feed, and DELETEs the one whose
    name equals the built version. This is the "overwrite" half of an idempotent
    re-publish: GitHub Packages rejects re-pushing an existing id+version, so the
    old version must be removed before :func:`publish_nupkgs` pushes the new one.

    A version that is not present (HTTP 404, or simply absent from the listing)
    is a no-op — overwrite is safe to run whether or not the version exists.

    Raises
    ------
    PublishError
        If ``gh`` is unavailable, *api_key* is empty, or a list/delete call
        fails for any reason other than the version already being gone. The
        caller maps this to exit code 5.
    """
    if not nupkgs:
        logger.info("No .nupkg files to overwrite; skipping NuGet delete.")
        return

    if not dry_run and not _gh_available():
        raise PublishError(
            "gh CLI not found on PATH. Install GitHub CLI before overwriting "
            "NuGet packages, or drop --nuget-overwrite."
        )
    if not dry_run and not api_key:
        raise PublishError(
            "No GitHub Packages token found for the NuGet overwrite. Export one "
            f"of {', '.join(_NUGET_TOKEN_ENV_VARS)} (a PAT with the "
            "delete:packages scope) before publishing, or drop --nuget-overwrite."
        )

    for nupkg in nupkgs:
        pkg_id, version = read_nupkg_identity(nupkg)
        list_cmd = build_nuget_list_versions_command(
            username=username, package_name=pkg_id
        )
        if dry_run:
            logger.info(
                "[dry-run] Would overwrite NuGet %s %s (list: %s; DELETE matching "
                "version).",
                pkg_id,
                version,
                " ".join(list_cmd),
            )
            continue

        result = subprocess.run(
            list_cmd,
            capture_output=True,
            text=True,
            env=_gh_env(api_key),
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            # A package that has never been published 404s — nothing to overwrite.
            if "404" in stderr or "Not Found" in stderr:
                logger.info(
                    "NuGet package %s not published yet; nothing to overwrite.",
                    pkg_id,
                )
                continue
            raise PublishError(
                f"Failed to list NuGet versions for '{pkg_id}': {stderr}"
            )

        try:
            versions = json.loads(result.stdout) if result.stdout.strip() else []
        except json.JSONDecodeError as exc:
            raise PublishError(
                f"Could not parse NuGet version listing for '{pkg_id}': {exc}"
            ) from exc

        version_id = next(
            (v.get("id") for v in versions if v.get("name") == version), None
        )
        if version_id is None:
            logger.info(
                "NuGet %s has no published version %s; nothing to overwrite.",
                pkg_id,
                version,
            )
            continue

        logger.info("Deleting existing NuGet %s %s to overwrite it.", pkg_id, version)
        delete_cmd = build_nuget_delete_version_command(
            username=username, package_name=pkg_id, version_id=version_id
        )
        delete_result = subprocess.run(
            delete_cmd,
            capture_output=True,
            text=True,
            env=_gh_env(api_key),
        )
        if delete_result.returncode != 0:
            raise PublishError(
                f"Failed to delete NuGet '{pkg_id}' version {version} "
                f"(id {version_id}): {delete_result.stderr.strip()}"
            )


def build_nuget_push_command(
    *,
    nupkg: str,
    source: str,
    api_key: str,
) -> list[str]:
    """Construct the ``dotnet nuget push`` command (no execution).

    Pure helper so tests can assert the exact command without invoking
    ``dotnet`` or touching the network.

    ``--skip-duplicate`` makes a re-publish idempotent: GitHub Packages returns
    409 Conflict for an already-uploaded id+version, and this flag turns that
    into a no-op instead of a hard failure — essential because the same package
    version is rebuilt on every run. ``--no-symbols`` stops dotnet from also
    pushing the adjacent ``.snupkg`` (GitHub Packages has no symbol server).
    """
    return [
        "dotnet",
        "nuget",
        "push",
        nupkg,
        "--source",
        source,
        "--api-key",
        api_key,
        "--skip-duplicate",
        "--no-symbols",
    ]


def publish_nupkgs(
    *,
    nupkgs: list[Path],
    source: str,
    api_key: str,
    dry_run: bool = False,
) -> None:
    """Push each package in *nupkgs* to the GitHub Packages NuGet feed *source*.

    Each package is pushed individually so a failure names the offending file.
    ``--skip-duplicate`` keeps re-runs idempotent.

    Raises
    ------
    PublishError
        If ``dotnet`` is unavailable, *api_key* is empty, or any push exits
        non-zero. The caller maps this to exit code 5.
    """
    if not nupkgs:
        logger.info("No .nupkg files to publish; skipping NuGet push.")
        return

    if not dry_run and not _dotnet_available():
        raise PublishError(
            "dotnet CLI not found on PATH. Install the .NET SDK before "
            "publishing NuGet packages, or pass --no-nuget to skip."
        )
    if not dry_run and not api_key:
        raise PublishError(
            "No GitHub Packages token found. Export one of "
            f"{', '.join(_NUGET_TOKEN_ENV_VARS)} (a PAT with the write:packages "
            "scope) before publishing, or pass --no-nuget to skip."
        )

    logger.info("Publishing %d NuGet package(s) to %s", len(nupkgs), source)
    for nupkg in nupkgs:
        cmd = build_nuget_push_command(nupkg=str(nupkg), source=source, api_key=api_key)
        if dry_run:
            # Never log the api-key. Print a redacted command instead.
            redacted = [a if a != api_key else "***" for a in cmd]
            logger.info("[dry-run] Would run: %s", " ".join(redacted))
            continue
        logger.info("Pushing %s", nupkg.name)
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as exc:
            raise PublishError(
                f"`dotnet nuget push` failed with exit code {exc.returncode} "
                f"for '{nupkg.name}'."
            ) from exc
