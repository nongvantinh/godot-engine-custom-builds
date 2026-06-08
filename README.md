# godot-build-scripts

Portable Python build orchestrator for a custom Godot engine fork. A single
cross-platform CLI (`build-godot.py`) is driven by a typed TOML configuration
file:

- host orchestration → [`scripts/host_orchestrator.py`](scripts/host_orchestrator.py)
- per-platform builds → [`scripts/in_container/build_<platform>.py`](scripts/in_container/)
- release packaging → [`scripts/packager.py`](scripts/packager.py)
- release publishing → [`scripts/orchestrator.py`](scripts/orchestrator.py) (`gh release`)

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [CLI Reference](#cli-reference)
3. [config.toml Key Reference](#configtoml-key-reference)
4. [Android signing](#android-signing)
5. [Android native debug symbols](#android-native-debug-symbols)
6. [Upstream Submodules](#upstream-submodules)
7. [Resumability](#resumability)
8. [Known limitations](#known-limitations)
9. [Patch System](#patch-system)
10. [Troubleshooting](#troubleshooting)

---

## Quick Start

### Prerequisites

- Python ≥ 3.11 (managed via [`uv`](https://github.com/astral-sh/uv))
- [Docker](https://docs.docker.com/get-docker/) (required for Windows, Android, Web; optional for Linux)
- A GitHub Personal Access Token stored in `GHCR_PAT` (for pulling private GHCR images)

### Clone and initialise submodules

```bash
git clone https://github.com/nongvantinh/godot-build-scripts.git
cd godot-build-scripts

# Initialise upstream submodules.
# WARNING: upstream/godot/ is large (~multi-GB).
# Use --depth=1 if you only need to build and do not need git log on engine history.
git submodule update --init --recursive          # full history
# — or —
git submodule update --init --recursive --depth=1  # shallow (build-only)
```

### Configure

```bash
cp config.toml.example config.toml
# Open config.toml and fill in your registry, username, image tags, etc.
$EDITOR config.toml
```

### Set secrets

```bash
# PAT for pulling images from ghcr.io (never put this in config.toml).
# Required scopes:
#   read:packages   — pull images from GHCR
#   write:packages  — push images to GHCR (containers --push)
#   repo            — publish GitHub Releases (release --upload)
export GHCR_PAT=<your-github-personal-access-token>

# gh CLI must be authenticated for `gh release create/upload`.
gh auth login
```

### Build container images

The Apple chain (`xcode -> osx -> ios`) extracts SDKs from a host-supplied
Xcode `.xip` placed at `containers/files/Xcode_<version>.xip`. The `.xip` is
gitignored and never committed. Place it before running `containers --type
xcode,osx,ios` (or `--type all`); the chain fails loud if it is missing.

```bash
# Build every image (base -> linux/windows/android/web/xcode/osx/ios) and push to GHCR.
uv run python build-godot.py containers --type all --version 4.7 --push
```

### Build

Builds are described as a **matrix** of `flavor × kind × mono × platform × arch`.
You declare *what* to build and the tool derives the SCons invocations — you no
longer hand-edit raw SCons flag strings.

```bash
# Linux Mono editor, release flavor, x86_64 only (Docker or local SCons fallback)
uv run python build-godot.py build \
  --platform linux --flavor release --kind editor --mono on --arch x86_64

# Full matrix for one platform (all flavors, both Mono + classical, all archs)
uv run python build-godot.py build --platform linux --mono both

# Every platform, Mono + classical, three flavors, full arch matrix
uv run python build-godot.py build --platform all --mono both

# Preview the derived SCons commands without running anything
uv run python build-godot.py build --platform all --mono both --dry-run
```

> macOS/iOS always build inside the `godot-osx` / `godot-ios` Docker images.
> If the Apple toolchain is not set up (`containers/files/Xcode_<ver>.xip`
> absent, or the `godot-osx` / `godot-ios` image was never built), the
> Apple build path fails loud with an actionable error — there is no
> silent skip and no auto-detection.

### Release (local build → package → publish)

The RAM-heavy SCons builds and the GitHub Release upload run **locally** on a
beefy host. The default `scons -j` is `nproc - 2` (leave 2 cores for the
system; floor of 1) — on a 16-core host that is `-j14`. CI only builds and
pushes the Docker images.

```bash
export GHCR_PAT=<token>                    # image pulls + GitHub Release (repo scope)
export GITHUB_PERSONAL_ACCESS_TOKEN=<token> # NuGet push (needs write:packages)
gh auth status                             # gh must be authenticated for the release upload

# One command: build the configured matrix, generate Mono glue once, package
# editor zips + .tpz (classical + mono) + version.txt + SHA512-SUMS.txt,
# publish a real GitHub Release on tag 4.7-dev1 (prerelease), and push the Mono
# NuGet packages (GodotSharp, GodotSharpEditor, Godot.SourceGenerators,
# Godot.NET.Sdk) to GitHub Packages.
# Omit --jobs to use the nproc-2 default; pass it to override (e.g. lower for
# RAM-heavy Mono passes).
uv run python build-godot.py release --jobs 14
```

The NuGet step runs after the Release upload. The Mono build emits the managed
packages into every `out/<plat>/<arch>/tools-mono/GodotSharp/Tools/nupkgs/`
dir; they used to ride along only inside the editor zip and were never pushed
to the feed. `release` now publishes one canonical copy of each (from the Linux
x86_64 build) to `https://nuget.pkg.github.com/<username>/index.json` with
`dotnet nuget push --skip-duplicate --no-symbols`. The token is read from
`GITHUB_PERSONAL_ACCESS_TOKEN` (then `GHCR_PAT`, then `GITHUB_TOKEN`) — any PAT
with the `write:packages` scope works. Toggle with `[release].publish_nuget` or
the `--no-nuget` flag; override the feed with `[release].nuget_source`.

**Overwriting an existing NuGet version.** GitHub Packages rejects re-pushing an
existing package id+version, so `--skip-duplicate` alone would *skip* a rebuilt
package and leave the stale one published. To make `release` actually replace it,
the NuGet step **overwrites by default** (`[release].nuget_overwrite = true`):
before pushing, it reads each package's id+version from the built `.nupkg`'s
nuspec, and `DELETE`s the matching version from the feed via
`gh api /users/<username>/packages/nuget/<pkg>/versions/<id>` (a no-op when the
version is not published yet). This deletion needs a token with the
`delete:packages` scope. Because `gh auth login` tokens usually carry only
`repo`/`workflow`, the delete calls run `gh` with `GH_TOKEN` set to the NuGet PAT
(`GITHUB_PERSONAL_ACCESS_TOKEN`/`GHCR_PAT`/`GITHUB_TOKEN`) so the
`delete:packages` scope is available. The Release-asset side is overwritten the
same way conceptually: `gh release upload ... --clobber` replaces same-named
assets when the Release already exists. Disable the NuGet overwrite with
`--no-nuget-overwrite` (push then skips existing versions via `--skip-duplicate`).

Resumable / partial runs:

```bash
# Re-publish without rebuilding (e.g. after a transient gh/nuget failure → exit 5)
uv run python build-godot.py release --no-build --no-package --upload

# Stop after producing artifacts (no GitHub Release, no NuGet push)
uv run python build-godot.py release --no-upload --no-nuget

# Push only the NuGet packages from an existing build (skip the Release upload)
uv run python build-godot.py release --no-build --no-package --no-upload --nuget
```

#### Consuming the published packages

The packages live in a **private** GitHub Packages feed, so any machine that
restores them must register the feed with a token that has the `read:packages`
scope. Add it once per device:

```bash
# GitHub Packages requires auth even for restore. On Linux there is no
# encrypted credential store, so --store-password-in-clear-text is required;
# the PAT is written to ~/.nuget/NuGet/NuGet.Config (chmod 0600).
dotnet nuget add source https://nuget.pkg.github.com/nongvantinh/index.json \
  --name github-nongvantinh \
  --username nongvantinh \
  --password "$GITHUB_PERSONAL_ACCESS_TOKEN" \
  --store-password-in-clear-text

# Verify the feed resolves a package:
dotnet package search GodotSharp --source github-nongvantinh   # may show nothing — GitHub's feed has no search service
curl -s -u "nongvantinh:$GITHUB_PERSONAL_ACCESS_TOKEN" \
  https://nuget.pkg.github.com/nongvantinh/download/godotsharp/index.json   # lists versions
```

This registers the feed at the **device** level (`~/.nuget/NuGet/NuGet.Config`)
so the PAT never lands in a committed file. A project that pins its own sources
with `<clear />` in `NuGet.config` must also add the `github-nongvantinh` source
key to that file for the build to see it (credentials are still resolved from
the device config by source name).

---

## CLI Reference

### Top-level

```
uv run python build-godot.py [--help] [--version] <sub-command>
```

### `build` sub-command

```
uv run python build-godot.py build [OPTIONS]
```

| Flag | Type | Required | Default | Description |
|---|---|---|---|---|
| `--platform` | `str` (comma-separated) | **Yes** | — | Target platform(s): `linux`, `windows`, `android`, `web`, `macos`, `ios`, or `all`. Multiple values are comma-separated, e.g. `linux,windows`. |
| `--flavor` | `str` (comma-separated) | No | from `[build].flavors` | `release`, `debug`, `release_debug`. Replaces `--target` for flavor selection. |
| `--kind` | `str` (comma-separated) | No | from `[build].kinds` | `editor`, `templates`. |
| `--mono` | `str` | No | from `[build].mono` | `on`, `off`, or `both`. |
| `--arch` | `str` (comma-separated) | No | from `[[platforms]].archs` | Restrict the arch matrix for a faster partial build. |
| `--jobs` | `int` | No | `[build].build_jobs` (nproc - 2) | SCons `-j` parallelism. |
| `--target` | `str` | No | _deprecated_ | Escape hatch: bypasses flavor/kind/mono derivation and is passed to SCons verbatim. |
| `--godot-repo` | `str` | No | `nongvantinh/godot` | GitHub slug of the Godot source repo. Pass `official` or `godotengine/godot` to use upstream. When `upstream/godot/` is initialised, its pinned commit is used directly. |
| `--config` | `path` | No | `./config.toml` | Path to the TOML configuration file. |
| `--verbose` | flag | No | off | Enable DEBUG-level logging. |
| `--dry-run` | flag | No | off | Print Docker / SCons commands without executing them. |

### `release` sub-command

Drive the full build → package → publish flow.

```
uv run python build-godot.py release [OPTIONS]
```

| Flag | Type | Required | Default | Description |
|---|---|---|---|---|
| `--config` | `path` | No | `./config.toml` | Path to the TOML configuration file. |
| `--build` / `--no-build` | flag | No | `--build` | Run the SCons builds, or reuse existing `out/`. |
| `--package` / `--no-package` | flag | No | `--package` | Run packaging, or reuse existing artifacts. |
| `--upload` / `--no-upload` | flag | No | from `[release].auto_upload` | Run `gh release` upload, or stop after producing artifacts. |
| `--nuget` / `--no-nuget` | flag | No | from `[release].publish_nuget` | Push the Mono NuGet packages to GitHub Packages after the Release upload. |
| `--nuget-overwrite` / `--no-nuget-overwrite` | flag | No | from `[release].nuget_overwrite` (`true`) | Delete an already-published NuGet id+version before pushing so the rebuilt package replaces it (needs `delete:packages`). `--no-nuget-overwrite` skips existing versions instead. |
| `--tag` | `str` | No | derived from `version.py` (e.g. `4.7.beta`) | Target tag for the Release. One-off override for hotfix re-publishes. |
| `--jobs` | `int` | No | `[build].build_jobs` (nproc - 2) | SCons `-j` parallelism. |
| `--godot-repo` | `str` | No | from config | GitHub slug of the Godot source repo. |
| `--dry-run` | flag | No | off | Print the build/package/`gh` commands without executing them. |

### `containers` sub-command

Build (and optionally push) Godot Docker container images from the local
`containers/` directory.

```
uv run python build-godot.py containers [OPTIONS]
```

| Flag | Type | Required | Default | Description |
|---|---|---|---|---|
| `--type` | `str` (comma-separated) | Yes (unless `--extract-sdks-only`) | — | Container type(s): `base`, `linux`, `windows`, `android`, `web`, `xcode`, `osx`, `ios`, or `all`. The Apple chain (`xcode` -> `osx` -> `ios`) needs `containers/files/Xcode_*.xip`. Multiple values are comma-separated. |
| `--version` | `str` | No | `godot_version` from config | Image version tag, e.g. `4.7`. |
| `--push` | flag | No | off | Push images to GHCR after building. Requires `GHCR_PAT` env var; skips gracefully when absent. Mutually exclusive with `--extract-sdks-only`. |
| `--extract-sdks-only` | flag | No | off | Skip image builds; only run the Apple SDK extraction step from `containers/files/Xcode_*.xip` (requires a built `godot-xcode:<version>` image). Useful for re-running extraction after a partial Apple build. |
| `--config` | `path` | No | `./config.toml` | Path to the TOML configuration file. |
| `--dry-run` | flag | No | off | Print Docker commands without executing them. |

#### Image naming

| Type | Local image | GHCR image |
|---|---|---|
| `base` | `godot-fedora:{version}` | `ghcr.io/{username}/godot-fedora:{version}` |
| `linux` | `godot-linux:{version}` | `ghcr.io/{username}/godot-linux:{version}` |
| `windows` | `godot-windows:{version}` | `ghcr.io/{username}/godot-windows:{version}` |
| `android` | `godot-android:{version}` | `ghcr.io/{username}/godot-android:{version}` |
| `web` | `godot-web:{version}` | `ghcr.io/{username}/godot-web:{version}` |

#### Build order

`base` is always built first. When any non-base type is requested, `base` is
automatically included and built before the other types.

#### Example invocations

```bash
# Build only the Linux container image
uv run python build-godot.py containers --type linux --version 4.7

# Build all images and push them to GHCR
export GHCR_PAT=<your-github-personal-access-token>
uv run python build-godot.py containers --type all --version 4.7 --push

# Build multiple types without pushing
uv run python build-godot.py containers --type linux,windows --version 4.7

# Dry run — print Docker commands without executing them
uv run python build-godot.py containers --type all --version 4.7 --dry-run
```

---

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Build completed successfully (including graceful Apple skips). |
| `1` | Configuration error (missing key, bad value, missing env var). |
| `2` | Platform not supported or container image not found in config. |
| `3` | Docker unavailable and local fallback is not possible. |
| `4` | Build/package/upload subprocess exited with non-zero status. |
| `5` | Release publish failed (`gh` auth missing, tag absent, asset conflict). Retriable with `release --no-build --no-package --upload`. |

### Example invocations

```bash
# Linux editor build (Docker, or local scons if Docker is unavailable)
uv run python build-godot.py build --platform linux --target editor

# Windows export templates (Docker required)
uv run python build-godot.py build --platform windows --target templates

# Multi-platform from the custom fork, non-default config
uv run python build-godot.py build \
    --platform linux,android \
    --godot-repo nongvantinh/godot \
    --config ci-config.toml

# Official Godot source, Web platform
uv run python build-godot.py build --platform web --godot-repo official

# Dry run — print Docker commands without running them
uv run python build-godot.py build --platform linux --dry-run
```

---

## `config.toml` Key Reference

Copy `config.toml.example` to `config.toml` and fill in your values.  
**Never commit `config.toml`** — it is listed in `.gitignore`.

```toml
# Registry configuration
registry = "ghcr.io"           # Container registry hostname
username = "nongvantinh"       # Registry username (image path prefix)

# Godot source / version
godot_version = "4.7"          # Used in SCons flags and image tags
git_branch = "4.7.dev1"        # Branch/treeish of godot_repo to build
godot_repo = "nongvantinh/godot"  # Fork slug (--godot-repo overrides)

[build]                        # The build matrix (flavor x kind x mono x arch)
flavors = ["release", "debug", "release_debug"]
kinds = ["editor", "templates"]
mono = ["on", "off"]           # ["on","off"] -> Mono + classical
archs = ["x86_64", "x86_32", "arm64", "arm32"]  # Optional global arch filter
# build_jobs = 14              # SCons -j (default nproc - 2; omit to track host)
xcode_sdkv = "26.1.1"          # Xcode version the .xip ships
apple_sdkv = "26.1"            # macOS SDK version (Xcode 26.1.1 -> 26.1)

[scons]
use_lto = false                # Link-time optimisation (increases build time)
extra_flags = ""               # Raw SCons flags appended to every build
accesskit_sdk_path = "/root/accesskit/accesskit-c"  # AccessKit C SDK path
redirect_build_objects = true  # Emit redirect_build_objects=no (upstream align)

[[platforms]]
name = "linux"
image = "ghcr.io/nongvantinh/godot-linux:4.7"
scons_flags = "platform=linuxbsd"      # platform-invariant flags only
archs = ["x86_64", "x86_32", "arm64", "arm32"]
env_setup = "export PATH=$GODOT_SDK_LINUX_X86_64/bin:$BASE_PATH"

[[platforms]]
name = "windows"
image = "ghcr.io/nongvantinh/godot-windows:4.7"
scons_flags = "platform=windows use_mingw=yes mingw_prefix=/root/llvm-mingw"
archs = ["x86_64", "x86_32", "arm64"]

# ... android, web, macos, ios entries — see config.toml.example ...

[release]
tag = "4.7-dev1"               # Existing tag the Release attaches to (no `v` prefix)
repo = "nongvantinh/godot-build-scripts"
auto_upload = true             # release sub-command runs gh release upload
draft = false
prerelease = true              # 4.7-dev1 is a dev build

[signing]
android_keystore = ""          # Absolute path to Android keystore (optional; operator-supplied)
android_key_alias = ""         # Android key alias (optional)
# android_key_password — NEVER here; use ANDROID_KEY_PASSWORD env var
```

> **Keystores MUST NOT be committed to this repo.** ``*.keystore`` is
> ``.gitignore``d. The operator supplies their own keystore file via
> ``[signing].android_keystore`` (an absolute path on the build host), the
> alias via ``[signing].android_key_alias``, and the password via the
> ``ANDROID_KEY_PASSWORD`` environment variable. See
> [Android signing](#android-signing) for the full setup flow.

### Key glossary

Top-level:

| Key | Type | Notes |
|---|---|---|
| `registry` | string | Container registry hostname (e.g. `ghcr.io`). |
| `username` | string | Registry namespace; image path prefix. |
| `godot_version` | string | Engine version string; threaded into SCons + image tags. |
| `git_branch` | string | Branch/treeish of `godot_repo` to build. |
| `godot_repo` | string | GitHub slug of the engine source repo. `--godot-repo` overrides. |

`[build]`:

| Key | Type | Default | Notes |
|---|---|---|---|
| `flavors` | list[string] | `["release","debug","release_debug"]` | Drives SCons `target=`/`production=` flags. |
| `kinds` | list[string] | `["editor","templates"]` | Editor binary vs. export template build. |
| `mono` | list[string] | `["on","off"]` | `["on","off"]` produces Mono + classical. |
| `archs` | list[string] | platform-defined | Optional global arch filter; intersected with per-platform `archs`. |
| `build_jobs` | int | `nproc - 2` | SCons `-j`; omit to track host. `--jobs` overrides. |
| `xcode_sdkv` | string | — | Xcode version the host-supplied `.xip` ships. |
| `apple_sdkv` | string | — | macOS SDK version inside that Xcode (e.g. `26.1` for Xcode 26.1.1). |

`[scons]`:

| Key | Type | Default | Notes |
|---|---|---|---|
| `use_lto` | bool | `false` | Link-time optimisation. |
| `extra_flags` | string | `""` | Raw SCons flags appended verbatim to every build. |
| `accesskit_sdk_path` | string | `/root/accesskit/accesskit-c` | In-container AccessKit C SDK path. |
| `redirect_build_objects` | bool | `true` | Emit `redirect_build_objects=no` (matches upstream pinned toolchain). |

`[[platforms]]` (one entry per platform):

| Key | Type | Notes |
|---|---|---|
| `name` | string | Platform identifier (`linux`, `windows`, `android`, `web`, `macos`, `ios`). |
| `image` | string | Full container image reference. |
| `scons_flags` | string | Platform-INVARIANT flags only; arch/target/Mono/flavor are derived. |
| `archs` | list[string] | Architectures supported by this platform. |
| `env_setup` | string | Optional shell snippet sourced before SCons (e.g. emsdk activation). |

`[release]`:

| Key | Type | Default | Notes |
|---|---|---|---|
| `tag` | string | — | Existing git tag the Release attaches to (e.g. `4.7-dev1`; matches the live tag — no `v` prefix). |
| `repo` | string | — | Repo slug to publish under (`owner/repo`). |
| `auto_upload` | bool | `true` | `release` sub-command runs `gh release create/upload`. |
| `draft` | bool | `false` | Publish as a draft Release. |
| `prerelease` | bool | `true` | Flag the Release as a prerelease (default for dev builds). |

> `scons_flags` per `[[platforms]]` now carries only **platform-invariant**
> flags. `arch`, `target`, Mono toggles, and flavor flags are derived from the
> `[build]` matrix and the per-platform `archs`.

**Secrets policy:** `GHCR_PAT` must be set as an environment variable — never
as a key in `config.toml`. `gh` authentication for publishing Releases also
lives in the environment. The tool will exit with code 1 if it detects a
`ghcr_pat` or `token` key anywhere in the config tree (including the new
`[build]` / `[release]` tables).

---

## Android signing

Android editor/template builds can be signed with an operator-supplied
keystore. **The build system never commits a keystore.** The previous
`data/godot-release.keystore` blob has been removed from the working tree
and `*.keystore` is now in `.gitignore`.

### Creating a keystore

```bash
keytool -genkeypair \
    -keystore /absolute/path/to/godot.keystore \
    -alias <your-alias> \
    -keyalg RSA -keysize 4096 -validity 10000
```

Keep this file off the repo. A common convention is to drop it under
`deps/keystore/` (which is gitignored as it lives under the build's deps
output), but any absolute path on the build host works.

### Wiring it into `config.toml`

```toml
[signing]
android_keystore = "/absolute/path/to/godot.keystore"
android_key_alias = "<your-alias>"
# android_key_password — NEVER in config.toml; use the env var below.
```

```bash
export ANDROID_KEY_PASSWORD=<your-keystore-password>
```

`build-godot.py` reads `ANDROID_KEY_PASSWORD` from the environment and
plumbs the keystore/alias into the Android build. If
`GODOT_ANDROID_SIGN_KEYSTORE` is set instead (legacy path), the host
orchestrator stages that file into `deps/keystore/` for the Android
container at build time.

### What MUST NOT be done

- **Never** commit a `.keystore` file (or any other signing material) to
  this repo. `.gitignore` enforces this at the working-tree level.
- **Never** add `android_key_password`, `ghcr_pat`, or any other secret
  key to `config.toml`. `build-godot.py` exits with code 1 if it detects
  one. Passwords/tokens live exclusively in environment variables.

If you previously cloned a revision that contained
`data/godot-release.keystore`, the file remains in git history. Rotating
the signing key on the Play Store / app distribution side is the
recommended mitigation regardless of any later history-rewrite
(`git filter-repo`) operation the operator may decide to plan separately.

---

## Android native debug symbols

The Android `template_release` matrix is built with native debug symbols so
crash stack traces from the Google Play Console / Firebase Crashlytics (or
`ndk-stack` locally) can be symbolicated. Following the Godot
[Resolving crashes on Android](https://docs.godotengine.org/en/latest/tutorials/platform/android/resolving_crashes_on_android.html)
guide, every arch is compiled with `debug_symbols=yes` and the final arch
(`x86_64`) additionally gets `separate_debug_symbols=yes` — that last build is
when SCons zips the accumulated `platform/android/java/lib/libs` tree (all four
arches) into `bin/android-template-release-native-symbols.zip`.

This works because the build runs SCons manually and then invokes gradle
(`generateGodotTemplates`) with the in-gradle SCons tasks excluded
(`excludeSconsBuildTasks()` is true without `-PgenerateNativeLibs`), so gradle
packages exactly the symbol-laden libs rather than recompiling them. The
packager publishes the zip as a standalone Release asset named
`godot-lib.<version>.template_release.native-symbols.zip`
(`...mono.template_release...` for the Mono flavor) — it is **not** bundled in
the `.tpz`, since it is a debugging aid, not a runtime template. Upload it to
the Play Console alongside the matching app build, or symbolicate manually with
the NDK's `ndk-stack`.

The symbols are produced only by a fresh Android build; a partial run whose
`out/android/` predates this is skipped with an INFO log. Delete
`out/android/` to force a rebuild that emits them.

---

## Resumability

The orchestrator skips a platform when its `out/<platform>/` directory is
already non-empty. This makes failed-mid-matrix re-runs cheap: only the
platforms that have not yet produced artifacts will be rebuilt.

To force a single platform to rebuild:

```bash
rm -rf out/<platform>/
uv run python build-godot.py release --jobs 14
```

The same gate applies to the Mono glue step: `mono-glue/` non-empty -> skip
glue regeneration, reuse the cached glue. Delete `mono-glue/` to force.

---

## Known limitations

- **Windows arm64 (best-effort)** — the prebuilt ANGLE arm64-LLVM bundle has
  a libc++ symbol clash with the MinGW toolchain we use. The build script
  marks the arm64 SCons pass as best-effort; releases ship only Windows
  x86_64 / x86_32 binaries until ANGLE publishes a rebuild without embedded
  libc++ symbols.
- **Web + Mono** — upstream Godot rejects `module_mono_enabled=yes` on the
  web platform (`modules/mono/config.py:14`). The build script honours the
  upstream gate and skips the web Mono pass; only the classical web editor
  is built.
- **Android editor APK/AAB** — only the `.aar` template library is wired up.
  The full editor APK/AAB build path is deferred to a follow-up ticket.

---

## Upstream Submodules

After `git submodule update --init --recursive`, three repos are populated
under `upstream/`:

| Path | Remote | Role |
|---|---|---|
| `upstream/godot-build-scripts/` | `godotengine/godot-build-scripts` | **Upstream-tracking.** Reference copy of upstream tooling. Read-only; not invoked. |
| `upstream/build-containers/` | `godotengine/build-containers` | **Upstream-tracking.** Reference copy of the official Dockerfiles. Read-only. |
| `upstream/godot/` | `nongvantinh/godot` | **Fork integration point.** The Godot engine source this Release builds. Engine-side fixes land here. |

### Submodule discipline

- The two upstream-tracking submodules
  (`upstream/godot-build-scripts` and `upstream/build-containers`) are kept
  in the tree purely as a reference for cherry-picking upstream changes.
  They are **not** invoked by the build path.
- `upstream/godot` is the **only** submodule that influences a Release. Its
  pointer is the engine source `build-godot.py` checks out and builds.
- **Never auto-repin** any of these three. Bumping a submodule pointer is an
  explicit operator action — it carries a Release-content change and goes
  through a normal PR. Tooling that pre-fetches submodules must not commit
  the resulting pointer drift.

### Updating submodules

```bash
# Bump upstream/godot to the latest fork commit (explicit operator action).
git -C upstream/godot fetch origin
git -C upstream/godot checkout <commit>
git add upstream/godot
git commit -m "chore(upstream): bump godot submodule to <commit>"
```

For the two upstream-tracking submodules the same flow applies; both should
be bumped only when there is a concrete reason (e.g. cherry-picking an
upstream fix).

---

## Patch System

Place unified-diff patch files (`*.patch`) in the `patches/` directory.
Before each build, `build-godot.py` applies them in lexicographic order
with `git apply --directory=<source_dir>`.

```
patches/
  001-disable-telemetry.patch
  002-custom-module.patch
```

Patches are applied to the Godot source in `upstream/godot/`. If `patches/`
is empty or contains no `*.patch` files, the step is a no-op.

---

## Troubleshooting

### Large submodule clone (`upstream/godot/`)

The `upstream/godot/` submodule downloads the full Godot engine history
(several GB). On a slow connection this can take 10–20 minutes. Use
`--depth=1` for a build-only shallow clone:

```bash
git submodule update --init --recursive --depth=1
```

To fetch full history later (for `git bisect` on engine commits):

```bash
git -C upstream/godot fetch --unshallow
```

### `GHCR_PAT not set`

```
ERROR: GHCR_PAT environment variable is not set.
```

Set your token before running the build:

```bash
export GHCR_PAT=<your-github-personal-access-token>
```

The token needs `read:packages` scope to pull images and `write:packages`
to push images.

### `Docker unavailable`

```
ERROR: Docker daemon is not running (docker info exited with 1).
```

- **Linux:** Start the daemon: `sudo systemctl start docker`
- **macOS / Windows:** Launch Docker Desktop.
- **Linux only:** If you do not want to use Docker, install `scons` locally
  and the tool will fall back to a native build automatically.

### `Platform 'X' not supported`

```
ERROR: Platform 'foobar' is not supported.
```

Supported platforms: `linux`, `windows`, `android`, `web`, `macos`, `ios`
(and `all`). macOS/iOS build inside the `godot-osx` / `godot-ios` images
and always attempt to build; if the Apple toolchain is not set up
(`containers/files/Xcode_<ver>.xip` absent, or the images were never
built), the Apple build path fails loud with an actionable error.

### `No [[platforms]] entry found for 'X'`

```
ERROR: No [[platforms]] entry found for 'android' in config.
```

Add the missing entry to your `config.toml`:

```toml
[[platforms]]
name = "android"
image = "ghcr.io/nongvantinh/android:4.3"
scons_flags = "platform=android"
```

### Apple SDK extraction failed

If `godot-osx` / `godot-ios` build fail with a missing `MacOSX*.sdk.tar.xz`
or `iPhoneOS*.sdk.tar.xz`, the SDK extraction step did not produce the
tarballs in `containers/files/`. Re-run extraction manually:

```bash
docker run --rm \
  -v "$(pwd)/containers/files:/root/files" \
  godot-xcode:<version> \
  /root/files/extract_xcode_sdks.sh
```

Confirm `containers/files/Xcode_<version>.xip` exists and matches
`[build].xcode_sdkv`. Then re-run the build with `--extract-sdks-only`:

```bash
uv run python build-godot.py containers --extract-sdks-only --version 4.7
```

### Resumability false-skip

If the orchestrator skips a platform you expected to rebuild, the
resumability gate sees a non-empty `out/<platform>/`. Delete it to force a
rebuild:

```bash
rm -rf out/<platform>/
uv run python build-godot.py release --jobs 14
```

The same applies to `mono-glue/` — delete it to force Mono glue
regeneration before the next Mono build.
