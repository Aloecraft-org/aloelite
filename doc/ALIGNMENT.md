# Versioning, artifacts and release alignment

**Status:** proposal, not yet applied to any repository.
**Applies to:** diluvium, diluvium-drt, dollup, aloelite, xtrshow, and any
Aloecraft project that publishes a release.

Every project here versions, names artifacts and publishes releases
differently. None of the differences were decided — they accumulated. This
document is the single shape to move toward, and a per-repository checklist
at the end saying what each one has to change.

Two rules in this document are load-bearing and were verified rather than
assumed. They are marked **Verified** and they should not be "simplified"
away; each one has a worked counter-example showing what breaks.

---

## 1. The version scheme

The **git tag is canonical**. Every other spelling derives from it
mechanically, so no two can drift.

```
v<major>.<minor>.<patch>[-<kind>.<n>]

kind ∈ dev | alpha | beta | rc
```

| git tag | PEP 440 (pyproject, PyPI) | SemVer (Cargo) | meaning |
|---|---|---|---|
| `v0.4.0-dev.7` | `0.4.0.dev7` | `0.4.0-dev.7` | a cheap build from a specific commit |
| `v0.4.0-alpha.1` | `0.4.0a1` | `0.4.0-alpha.1` | alpha |
| `v0.4.0-beta.2` | `0.4.0b2` | `0.4.0-beta.2` | beta |
| `v0.4.0-rc.1` | `0.4.0rc1` | `0.4.0-rc.1` | release candidate |
| `v0.4.0` | `0.4.0` | `0.4.0` | the release |

All five order correctly in both ecosystems:

```
0.4.0.dev7  <  0.4.0a1  <  0.4.0b2  <  0.4.0rc1  <  0.4.0
```

No local-version segments (`+104`). PyPI rejects them outright.

### Verified: do not spell a build suffix `-b<n>`

`b` is PEP 440's beta marker, and the hyphen is normalised away:

```python
>>> from packaging.version import Version
>>> Version('0.0.1-b104')
<Version('0.0.1b104')>
>>> Version('0.0.1-beta104')
<Version('0.0.1b104')>
>>> Version('0.0.1-b104') == Version('0.0.1-beta104')
True
```

A build number spelled `-b104` *is* beta 104, indistinguishable from one.
`dev` is the slot that already means "a build before the release", and it
sorts below every other prerelease. Use it.

### Verified: the dot before the number is not decoration

SemVer compares dot-separated identifiers — numeric ones numerically,
alphanumeric ones lexically. A suffix with no dot is one alphanumeric blob:

```
b104     <  b2       ← build 104 sorts BEFORE build 2
dev.104  >  dev.2    ← the dot makes 104 a numeric identifier
```

Write `-dev.7`, never `-dev7`. The bug only appears at build 10 and then
silently misorders everything after it.

### Never put an upstream version in your version string

**Rule:** an upstream version is recorded as a fact, never encoded in your
own version number.

**The test:** *can you ship a fix without the upstream moving?* If yes, and
your version has nowhere to go, the coupling is in the wrong place.

diluvium's `v5.5.1_build14` is that failure written down fourteen times —
fourteen releases that had something to say and no digit free to say it in,
because all three belong to Lua. DRT already resolved the same problem and
wrote the principle down in `doc/Release.md`:

> DRT versions independently of diluvium. The coupling is RECORDED, not
> required: `BUILDINFO.txt` in every release names the diluvium git revision
> this binary embeds and the dv ABI version it speaks, so "which diluvium is
> inside" is a fact you read off the release, never a tag-naming convention.

Record upstream coordinates as changelog fields and `BUILDINFO.txt` lines.
diluvium already does this — every entry carries `lua_base: 5.5.1` and
`bytecode_format: 70`, and the release mirror already renders them. The Lua
version is in the system twice; drop the welded copy, keep the recorded one.

### Compatibility is checked by name, never by digits

A version number is a label for humans and an ordering for tools. Anything a
consumer must actually *verify* — an ABI number, a bytecode format, a
connector or feature set — is a named field in `BUILDINFO.txt` and in the
changelog entry, and the check compares those fields.

DRT's `doc/Release.md` states it directly: *"the digit is not the check — the
connector list is, by name."* This is also what lets a patch-level release be
honest when a connector is added.

---

## 2. `.technoproj`

`.technoproj` holds the only version numbers a human edits. Every repository
gets one, including those that do not have one today.

```json
{
  "TECHNO_VERSION": {
    "major": 0,
    "minor": 4,
    "patch": 0,
    "pre": { "kind": "rc", "n": 1 }
  },
  "TECHNO_CHANGELOG": { "...see section 3..." },
  "TECHNO_INIT_DIRS": [".nogit", ".dump", "script"],
  "TECHNO_COPYRIGHT": "Copyright Michael Godfrey [2026] | aloecraft.org [michael@aloecraft.org]"
}
```

`pre` is `null` for a release, or `{"kind": ..., "n": ...}` for a prerelease.

**This replaces the old `build` field**, which currently means three
different things and is typed two different ways:

| repo | `build` today | what it means there |
|---|---|---|
| aloelite | `0` / `"rc1"` | a PEP 440 suffix, concatenated with no separator |
| xtrshow | `0` | an off switch |
| diluvium | `"14"` | a build counter (which under aloelite's rule renders `5.5.114`) |

`.technoproj` **cannot** hold the commit hash or branch — committing the file
changes the hash it claims. Those are discovered at build time and go in
`BUILDINFO.txt` (section 5).

---

## 3. Shared tooling

`changelog.py` and `version.mk` become one shared copy. Today `changelog.py`
is 423 / 484 / 576 lines in diluvium / DRT / aloelite — one tool, copied
twice, then drifted. `version.mk` is byte-identical in aloelite and xtrshow,
which is the same story one step earlier.

> **Open:** the shared location is not yet decided. Until it is, treat the
> engine as vendored at `script/changelog.py` and `script/version.mk`.

The divergence between the three copies turned out to be a **schema and a
fact table, not logic**. So what differs per repository is *declared*, in a
`TECHNO_CHANGELOG` block in `.technoproj`:

```json
"TECHNO_CHANGELOG": {
  "project": "DRT",
  "intro_extra": "\nDRT versions independently of diluvium...\n",
  "facts": [
    {"id": "dv_abi", "keys": ["dv_abi"], "fmt": "dv ABI {dv_abi}"},
    {"id": "dil", "keys": ["diluvium", "diluvium_build"],
     "fmt": "diluvium `{diluvium!s:.12}` (build{diluvium_build})"},
    {"id": "dil", "keys": ["diluvium"], "fmt": "diluvium `{diluvium!s:.12}`"}
  ],
  "mappings": [
    {"key": "connectors", "title": "Connectors"},
    {"key": "features", "title": "Core features"}
  ],
  "tag_rule": "exact",
  "emit_json": true,
  "stamps": [
    {"file": "Cargo.toml", "find": "version\\s*=\\s*\"(\\d+\\.\\d+\\.\\d+)\"",
     "spelling": "base"}
  ]
}
```

| field | meaning |
|---|---|
| `project` | display name in the generated `CHANGELOG.md` preamble |
| `intro_extra` | repo-specific text after the Keep a Changelog line |
| `facts` | the compatibility facts this project records, in render order. A fact renders when every key it names is present; the first variant matching a given `id` wins |
| `mappings` | profile → list blocks (DRT's connectors and features) |
| `tag_rule` | `exact` (tag is `v{version}`), `prefix` (must start with `v`), `derive` (fall back to `v{version}`) |
| `required` | keys every entry must carry |
| `latest_requires` | keys the `latest: true` entry must carry |
| `candidates`, `planned` | enable aloelite's release-candidate lists and `planned` section |
| `emit_json` | whether `changelog.json` is generated — required for a `source: changelog` release mirror |
| `stamps` | generic version-location checks: a file, a pattern, and which spelling it should hold |

**What stays local.** Genuinely bespoke invariants do not generalise and
should not be forced into config. Each repository keeps a `script/checks.py`
exposing `consistency(doc, ctx) -> list[str]`, which the engine calls if it
exists. `ctx` carries `read`, `root` and `base`. These are the current ones:

- **diluvium** — `LUAC_FORMAT` in `src/lundump.h`, `LUA_VERSION_*` in
  `src/lua.h`, the `VERSION` file
- **diluvium-drt** — the diluvium git revision pinned in `Cargo.lock`
  matching the changelog's `diluvium` field
- **aloelite** — `SCHEMA_ERA` in `aloelite/db.py`

Porting them is mechanical: the existing function bodies move across
unchanged.

**The consolidation is verified.** The shared engine reproduces every
committed output byte-for-byte:

```
diluvium       CHANGELOG.md    112,770 bytes   identical
               changelog.json  234,253 bytes   identical
diluvium-drt   CHANGELOG.md    173,306 bytes   identical
               changelog.json  362,887 bytes   identical
aloelite       CHANGELOG.md     33,001 bytes   identical
```

If your repository's output changes after adopting the engine, the
declaration is wrong — not the engine.

---

## 4. Artifact naming

```
<project>[_<component>]_<os>_<arch>[_<libc>][_<profile>][.<ext>]
```

Fields separated by `_`, read positionally. Examples:

```
dollup_darwin_arm64
drt_linux_x86_64_musl
drt_linux_x86_64_musl_slim
drt_windows_x86_64_slim.exe
diluvium_compiler_linux_x86_64_musl
aloelite_fuse_linux_aarch64_gnu
drt_wasi.wasm
drt_web.tar.gz
```

### The version does not go in the filename

This is not a matter of taste. The release mirror materialises `latest/` as a
**symlink to the tag directory**, so:

```
https://software.aloecraft.org/releases/diluvium-drt/latest/drt_linux_x86_64_musl
```

is a URL that never changes. dollup's `install.sh` and diluvium-lab's channel
default both depend on that. Put the version in the filename and `latest/`
buys nothing — every consumer goes back to parsing `releases.json` to
discover a URL.

aloelite's current `aloelite-0.4.0-x86_64-unknown-linux-gnu.tar.gz` is the
shape that breaks this.

### A fixed platform vocabulary, not target triples

Rust triples are precise, unreadable, and vary by toolchain. Use:

- **os** — `linux`, `darwin`, `windows`
- **arch** — `x86_64`, `aarch64`, `armv7`
- **libc**, where it matters — `gnu`, `musl`
- wasm targets are their own leaf: `_wasi.wasm`, `_web.tar.gz`

Note that `static`, as in today's `linux_static_x86_64`, is a link mode
rather than a platform. `musl` says the same thing in a slot that exists.

### The name is a handle, not a specification

Keep names short enough to type and glob. Everything the name cannot carry
goes in `BUILDINFO.txt`, and **BUILDINFO is what gets checked** — never the
filename.

DRT already demonstrates the leak: `drt_slim_windows_x86_64.exe` is the only
Windows build there is, and the name cannot say so; a comment in the workflow
says it instead. Its package admission already checks `requires.connectors`
against BUILDINFO by name rather than reading the filename. That is the
pattern — one `profile` token in the name as a human handle, the
authoritative sets in BUILDINFO beside it.

---

## 5. `BUILDINFO.txt`

Every release ships one, as a release asset, listed in `SHA256SUMS.txt`.
It carries what `.technoproj` cannot and what the filename should not:

```
tag: v0.4.0-rc.1
version: 0.4.0rc1
commit: 3f9a1c7e2b884d05a1e6f0c9b7d4e2a8f1c33b90
branch: main
built: 2026-09-12T18:20:08Z
```

…plus this project's own compatibility facts, the same ones its changelog
entry records. DRT's per-profile connector and feature lines are the model.

dollup and DRT already emit a `BUILDINFO.txt`; their field sets are unrelated
to each other, and the five lines above are the common floor.

---

## 6. `SHA256SUMS.txt`

**The filename is `SHA256SUMS.txt`.** Not `SHA256SUMS`.

It covers every other release asset, including `BUILDINFO.txt`, and is
published as a release asset itself:

```sh
cd release_dist && sha256sum * > SHA256SUMS.txt
```

(Generate it last, so the glob does not include it.)

The release mirror verifies every mirrored file against this manifest and
refuses the tag on a mismatch. A release publishing no manifest is mirrored
*self-attested* and says so on its index page.

aloelite currently writes `SHA256SUMS` with no extension. That single missing
extension is the only reason its release mirror is disabled.

---

## 7. Dev builds and nightlies

A `-dev.<n>` tag exists to get a specific commit into someone's hands without
a ten-minute gate and without a hash in the version string. The hash is not
missing — it is in `BUILDINFO.txt`, which is exactly what lets the version
stay short.

### The number is allocated from existing tags

Not stored in `.technoproj`. A counter in the tree would mean a commit on
every nightly, and two branches could collide on the same number.

```sh
git tag --list 'v*-dev.*' \
  | sed -n 's/.*-dev\.\([0-9][0-9]*\)$/\1/p' \
  | sort -n | tail -1 | awk '{print $1+1}'
```

`make dev-tag` in the shared `version.mk` prints the next free tag. The
counter is global and monotonic per repository and is never reused, so
`dev.105` names exactly one build in that repository's history forever.
Ordering still works across versions because the release segment dominates:
`0.4.0.dev104 < 0.5.0.dev105`.

### The suffix decides the rigor

A `dev` tag builds one platform, skips cross-compilation, wasm and the slow
suites. Everything else runs the full gate.

This is better than a dispatch checkbox — diluvium's `run_tests` input is the
only fast path any repository has today, and what it decided is invisible
once the run is over. A version string travels with the artifact.

### Publishing

- `prerelease: true`, always.
- Nightly: once every 24 hours, and **skip if HEAD is unchanged** since the
  last dev tag, or identical builds accumulate.
- Manual: a `workflow_dispatch` with a `ref` input, so any branch can be cut.
- Prune your own old dev releases. A dev build worth keeping is worth an `rc`.

### Mirroring

Dev builds do **not** go in the project's main release mirror. Two concrete
reasons: `MIRROR_KEEP` is 10, so a run of dev builds evicts real releases from
the tree; and `latest-prerelease/` symlinks to the newest non-stable tag, so
dev builds would stop it meaning "the newest release candidate".

They go in a separate nightly mirror entry instead, which keeps its own
retention budget. That is configured on the lk2 side, not here — nothing in
a project repository changes for it beyond publishing the tag.

---

## 8. Per-repository checklist

### diluvium

- [ ] **Its own version line.** `5.5.1_build14` → a version diluvium owns.
      `lua_base` and `bytecode_format` stay recorded facts — they already are.
- [ ] `.technoproj`: `build: "14"` → `pre`
- [ ] `VERSION` file: reconcile with the new scheme or retire it
- [ ] Artifact names: `diluvium_linux_static_x86_64` →
      `diluvium_linux_x86_64_musl`, and the same for `_compiler`, `_host`,
      `_rest_plugin`
- [ ] Adopt the shared engine; move `LUAC_FORMAT` / `LUA_VERSION_*` /
      `VERSION` checks into `script/checks.py`
- [ ] Keep `tag_rule: "prefix"` — upstream Lua's tags share this namespace

Already conforms: `changelog.json`, `SHA256SUMS.txt`, a fast path
(`run_tests`).

### diluvium-drt

- [ ] Tag spelling: `v0.5.0rc9` → `v0.5.0-rc.9`
- [ ] Gains a `.technoproj` (it has none)
- [ ] Artifact names: profile moves last —
      `drt_slim_linux_static_x86_64` → `drt_linux_x86_64_musl_slim`;
      `drt_slim_windows_x86_64.exe` → `drt_windows_x86_64_slim.exe`
- [ ] Adopt the shared engine; move the `Cargo.lock` diluvium-pin check into
      `script/checks.py`
- [ ] Add a `dev` fast path — `publish` currently needs test, build,
      build-wasip2, build-web, build-windows and smoke-windows

Already conforms: `changelog.json`, `SHA256SUMS.txt`, `BUILDINFO.txt`,
records its upstream coupling correctly.

### dollup

- [ ] **Fix the drift:** `Cargo.toml` says `0.0.2`, the only tag is `v0.0.1`
- [ ] Gains `.technoproj`, `version.mk`, `CHANGELOG.yaml` and the engine
- [ ] **`release.yml` sets no `prerelease` flag and uses
      `generate_release_notes: true`.** Its mirror is `source: github`, where
      GitHub's prerelease flag decides stable — so every dollup release
      currently publishes as stable with an autogenerated commit list. Derive
      both from the changelog.
- [ ] Artifact names: `dollup_linux_static_x86_64` → `dollup_linux_x86_64_musl`

Already conforms: `SHA256SUMS.txt`, `BUILDINFO.txt`, `install.sh` as an asset.

### aloelite

- [ ] **`SHA256SUMS` → `SHA256SUMS.txt`.** This alone unblocks its release
      mirror.
- [ ] Artifact names: drop the version and the Rust triples.
      `aloelite-0.4.0-x86_64-unknown-linux-gnu.tar.gz` →
      `aloelite_linux_x86_64_gnu.tar.gz`
- [ ] `.technoproj`: `build: 0` → `pre: null`
- [ ] Version spelling: `v0.4.0rc1` → `v0.4.0-rc.1` (and `rust/Cargo.toml`
      `0.4.0-rc.1`, unchanged in form)
- [ ] Adopt the shared engine; move the `SCHEMA_ERA` check into
      `script/checks.py`
- [ ] Set `emit_json: true` if it should be mirrored from its changelog
      rather than from GitHub — it generates no `changelog.json` today
- [ ] Decide whether `release` should gate on tests. It currently depends on
      `[plan, python, native, wasm, image]` and no test job.

Already conforms: `CHANGELOG.yaml`, a working `pep440_to_semver`, release
notes from the changelog.

### xtrshow

- [ ] **It has no release workflow** — only `publish.yml` to PyPI. If xtrshow
      should ever be mirrored, it needs one that produces artifacts,
      `BUILDINFO.txt` and `SHA256SUMS.txt`. If it should stay PyPI-only, say
      so in `mirrors.json` and close the entry rather than leaving it
      "waiting on the repo".
- [ ] `.technoproj`: `build: 0` → `pre: null`
- [ ] Gains `CHANGELOG.yaml` and the engine

Already conforms: `.technoproj`, `version.mk` (byte-identical to aloelite's),
PyPI publishing with trusted publishing.

---

## 9. Summary of what is non-negotiable

Everything else in this document is a default you can argue with. These are
the ones where a deviation breaks something concrete:

1. `-dev.<n>` with the dot, never `-b<n>` or `-dev<n>` — **§1, both verified**
2. No version in artifact filenames — breaks `latest/` URLs — **§4**
3. `SHA256SUMS.txt`, with the extension — the mirror looks for that name — **§6**
4. Upstream versions recorded, never encoded — **§1**
5. Dev builds flagged `prerelease: true` and kept out of the main mirror — **§7**
