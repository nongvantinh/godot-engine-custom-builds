# Changelog

Released versions of `nongvantinh/godot-build-scripts`. Each entry corresponds to a
[GitHub Release](https://github.com/nongvantinh/godot-build-scripts/releases) tag and
records the toolchain changes that produced the published engine artifacts.

## [4.7-dev1] — 2026-05-29

First real GitHub Release of this fork. The toolchain itself was rewritten from Bash
into a portable Python orchestrator in the same change; the Release attaches the
Mono and classical editor binaries plus export templates produced by that orchestrator
to the (previously empty) `4.7-dev1` tag.

### Toolchain

- Replaced the legacy `build-godot-and-templates/{main.sh,prepare_release.sh,build-*/build.sh}`
  Bash chain with `scripts/host_orchestrator.py`, `scripts/in_container/*.py`, and
  `scripts/packager.py`. The Python CLI now runs Python modules inside the build
  containers instead of shelling out to Bash.
- Single CLI entry point: `build-godot.py` with sub-commands `containers`, `build`,
  and `release`. Driven by typed TOML configuration (`config.toml`).
- First-class build matrix in `config.toml`: `flavor × kind × mono × platform × arch`,
  with no hand-edited SCons flag strings.
- Mono glue generated once per Release and reused across all Mono platform/arch
  builds, guaranteeing internal consistency.
- Resumability gate: a finished platform's non-empty `out/<platform>/` skips its
  re-build. Delete the directory to force a fresh build.
- Apple toolchain: `godot-xcode → godot-osx → godot-ios` image chain bootstrapped
  from a host-supplied `containers/files/Xcode_<ver>.xip` (gitignored, never
  committed). macOS/iOS always attempt to build; a missing toolchain fails the
  build loudly — no silent skip.
- Secrets policy: `config.toml` is recursively validated to reject any `ghcr_pat`
  or `token` key; credentials must flow through `GHCR_PAT` env + `gh auth`.
- Test suite: 213 → 335 passing (+122).

### Release artifacts (22 total, ~3.5 GB)

Published on tag [`4.7-dev1`](https://github.com/nongvantinh/godot-build-scripts/releases/tag/4.7-dev1).

- Classical editor binaries: Linux (x86_64, x86_32, arm64, arm32), Windows
  (x86_64, x86_32), macOS (universal), Web.
- Mono editor binaries: Linux (x86_64, x86_32, arm64, arm32), Windows (x86_64,
  x86_32), macOS (universal).
- Export-template bundles: `Godot_v4.7-dev1_export_templates.tpz` (classical) and
  `Godot_v4.7-dev1_mono_export_templates.tpz` (Mono).
- Android template libraries: `godot-lib.4.7.dev1.template_release.aar` (classical)
  and `godot-lib.4.7.dev1.mono.template_release.aar` (Mono).
- Checksums: `SHA512-SUMS.txt` (classical) and `SHA512-SUMS-mono.txt` (Mono).

Verify a download with:

```bash
sha512sum -c SHA512-SUMS.txt        # classical
sha512sum -c SHA512-SUMS-mono.txt   # Mono
```

### Container images

Published to [`ghcr.io/nongvantinh`](https://github.com/nongvantinh?tab=packages&repo_name=godot-build-scripts):
`godot-fedora`, `godot-linux`, `godot-windows`, `godot-android`, `godot-web`,
`godot-xcode`, `godot-osx`, `godot-ios`, all tagged `:4.7`.

### Known limitations (carry forward as separate follow-up tickets)

- **Windows arm64** — prebuilt ANGLE arm64-LLVM has a libc++ symbol clash; not
  included in this Release.
- **Web Mono** — Godot upstream rejects `module_mono_enabled=yes` on web
  (`modules/mono/config.py:14`); the build honours the upstream gate.
- **Android editor APK/AAB** — only the `.aar` template library is built; the
  editor APK/AAB build path is deferred.
- **Code signing** — macOS/iOS artifacts are unsigned. Apple distribution will
  require notarization in a future Release.

### Links

- Pull request: [#11](https://github.com/nongvantinh/godot-build-scripts/pull/11)
- Ticket: [#10](https://github.com/nongvantinh/godot-build-scripts/issues/10)
- Toolchain commit: [`cc7f40c`](https://github.com/nongvantinh/godot-build-scripts/commit/cc7f40cf34da4624300f851d5f6554652101cec9)
- Release: <https://github.com/nongvantinh/godot-build-scripts/releases/tag/4.7-dev1>

[4.7-dev1]: https://github.com/nongvantinh/godot-build-scripts/releases/tag/4.7-dev1
