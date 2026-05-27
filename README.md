# godot-build-scripts

Portable Python build orchestrator for a custom Godot engine fork.  
Replaces the old Bash scripts (`main.sh`, `shared.sh`, `config.sh`) with a
single cross-platform CLI (`build-godot.py`) driven by a typed TOML
configuration file.

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [CLI Reference](#cli-reference)
3. [config.toml Key Reference](#configtoml-key-reference)
4. [Upstream Submodules](#upstream-submodules)
5. [Patch System](#patch-system)
6. [config.sh → config.toml Migration](#configsh--configtoml-migration)
7. [Troubleshooting](#troubleshooting)

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
# PAT for pulling images from ghcr.io (never put this in config.toml)
export GHCR_PAT=<your-github-personal-access-token>
```

### Build

```bash
# Linux editor (Docker or local SCons fallback)
uv run python build-godot.py build --platform linux --target editor

# Windows export templates (Docker required)
uv run python build-godot.py build --platform windows --target templates

# Multi-platform
uv run python build-godot.py build --platform linux,android --target editor
```

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
| `--platform` | `str` (comma-separated) | **Yes** | — | Target platform(s): `linux`, `windows`, `android`, `web`. Multiple values are comma-separated, e.g. `linux,windows`. |
| `--target` | `str` | No | `editor` | SCons build target: `editor`, `templates`, `template_debug`, `template_release`, or any raw SCons target string. |
| `--godot-repo` | `str` | No | `nongvantinh/godot` | GitHub slug of the Godot source repo. Pass `official` or `godotengine/godot` to use upstream. When `upstream/godot/` is initialised, its pinned commit is used directly. |
| `--config` | `path` | No | `./config.toml` | Path to the TOML configuration file. |
| `--verbose` | flag | No | off | Enable DEBUG-level logging. |
| `--dry-run` | flag | No | off | Print Docker commands without executing them. |

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Build completed successfully. |
| `1` | Configuration error (missing key, bad value, missing env var). |
| `2` | Platform not supported or container image not found in config. |
| `3` | Docker unavailable and local fallback is not possible. |
| `4` | Build subprocess exited with non-zero status. |

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

# Godot version string (used in SCons flags and image tags)
godot_version = "4.3"

[scons]
use_lto = false                # Link-time optimisation (increases build time)
extra_flags = ""               # Raw SCons flags appended to every build

[[platforms]]
name = "linux"
image = "ghcr.io/nongvantinh/linux-editor:4.3"
scons_flags = "platform=linuxbsd"

[[platforms]]
name = "windows"
image = "ghcr.io/nongvantinh/windows:4.3"
scons_flags = "platform=windows"

[[platforms]]
name = "android"
image = "ghcr.io/nongvantinh/android:4.3"
scons_flags = "platform=android"

[[platforms]]
name = "web"
image = "ghcr.io/nongvantinh/web:4.3"
scons_flags = "platform=web"

[signing]
android_keystore = ""          # Path to Android keystore file (optional)
android_key_alias = ""         # Android key alias (optional)
# android_key_password — NEVER here; use ANDROID_KEY_PASSWORD env var
```

**Secrets policy:** `GHCR_PAT` must be set as an environment variable — never
as a key in `config.toml`. The tool will exit with code 1 if it detects a
`ghcr_pat` or `token` key in the config file.

---

## Upstream Submodules

After `git submodule update --init --recursive`, three repos are populated
under `upstream/`:

| Path | Remote | Purpose |
|---|---|---|
| `upstream/godot-build-scripts/` | `godotengine/godot-build-scripts` | Official SCons helper scripts (full history) |
| `upstream/build-containers/` | `godotengine/build-containers` | Official Dockerfiles (full history) |
| `upstream/godot/` | `nongvantinh/godot` | Custom Godot fork — default build source |

### Updating submodules to latest upstream

```bash
# Update all submodules to the latest commit on their tracking branch
git submodule update --remote --merge

# Update a single submodule
git -C upstream/godot pull origin main

# Commit the updated submodule pointer
git add upstream/godot
git commit -m "chore(upstream): bump godot submodule to latest"
```

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

## `config.sh` → `config.toml` Migration

| Old key (`config.sh`) | New key (`config.toml`) | Notes |
|---|---|---|
| `REGISTRY` | `registry` | Top-level string |
| `USERNAME` | `username` | Top-level string |
| `GODOT_VERSION` | `godot_version` | Top-level string |
| `USE_LTO` | `scons.use_lto` | Boolean under `[scons]` |
| `EXTRA_FLAGS` | `scons.extra_flags` | String under `[scons]` |
| `LINUX_IMAGE` | `platforms[name="linux"].image` | Under `[[platforms]]` |
| `WINDOWS_IMAGE` | `platforms[name="windows"].image` | Under `[[platforms]]` |
| `ANDROID_IMAGE` | `platforms[name="android"].image` | Under `[[platforms]]` |
| `WEB_IMAGE` | `platforms[name="web"].image` | Under `[[platforms]]` |
| `GHCR_PAT` / `PAT_TOKEN` | **env var only** — `GHCR_PAT` | Never in `config.toml` |

Replace any invocations of `sudo bash main.sh godot` with:

```bash
uv run python build-godot.py build --platform <target> --target editor
```

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
ERROR: Platform 'macos' is not supported.
```

Currently supported platforms: `linux`, `windows`, `android`, `web`.  
macOS cross-compilation is not supported without an Xcode host.

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
