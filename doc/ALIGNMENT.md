# Versioning, artifacts and release alignment

**Revision 3.** Status: proposal, not yet applied to any repository.
**Applies to:** diluvium, diluvium-drt, diluvium-lab, dollup, aloelite,
xtrshow, aloeschema.

Every project here versions, names artifacts and publishes releases
differently. None of the differences were decided — they accumulated. This
document is the single shape to move toward, and a per-repository checklist
at the end saying what each one has to change.

Rules marked **Verified** were checked against a real toolchain, not assumed.
Each carries a worked counter-example showing what breaks. They should not be
"simplified" away.

### What changed in revision 3

- **§1** settles what each project's next version is: **no synchronised
  1.0.0** — every project's minor increments from where it already is. This
  removes the decision that was gating diluvium and, through it, DRT.
- **diluvium-lab** is added. It is the second consumer that breaks on
  diluvium's change, and it breaks on an axis §4 had not covered.
- **§4** adds the rule that renaming an artifact breaks anything fetching it
  by name — with the Lab as the worked example.

### What changed in revision 2

Every project sent back review. Nine things in revision 1 were wrong,
ambiguous or missing, and all nine came from that review:

- **§1** now says which spelling the `version` field holds. Revision 1 gave
  PEP 440 in one place and SemVer in another and could not hold for a Rust
  repo. *(diluvium-drt)*
- **§1** adds **existing tags are never respelled**. Revision 1 implied
  retagging, which nobody should do. *(aloelite)*
- **§4** settles `arm64` vs `aarch64`. Revision 1 used both. *(diluvium-drt)*
- **§4** adds per-binary *and* `complete` artifacts. *(aloelite, decided)*
- **§4** carves out wheels and sdists — PEP 427 puts the version in the
  filename and pip parses it. Affects three repos. *(aloeschema, xtrshow)*
- **§7 / §8** adds the PyPI hazard: a `-dev.` tag publishes to PyPI as a
  valid version, and PyPI uploads are immutable. *(aloeschema, xtrshow)*
- **§9** is new: test gating and a tag ↔ version gate, both of which several
  repos lack. *(xtrshow, aloelite)*
- **§10** is new: migration order across repos, and what breaks mid-flight.
  *(diluvium-drt, aloeschema)*
- **aloeschema** is added, with the highest-priority item in the document.

---

## 1. The version scheme

The **git tag is canonical**. Every other spelling derives from it, so no two
can drift.

```
v<major>.<minor>.<patch>[-<kind>.<n>]

kind ∈ dev | alpha | beta | rc
```

### The `version` field holds the tag body

Revision 1 was ambiguous here and could not be satisfied. To be exact:

| where | spelling | example |
|---|---|---|
| git tag | canonical | `v0.4.0-rc.1` |
| `CHANGELOG.yaml` `version` | **tag body** — the tag without its `v` | `0.4.0-rc.1` |
| `Cargo.toml` | identical to the tag body | `0.4.0-rc.1` |
| `pyproject.toml` | **derived**, PEP 440 | `0.4.0rc1` |
| `BUILDINFO.txt` `version:` | the tag body | `0.4.0-rc.1` |

So `tag_rule: exact` — tag equals `v` plus the entry's `version` — holds for
every project, Rust and Python alike. PEP 440 is a *derived* spelling that
exists only in `pyproject.toml` and on PyPI; it is never the canonical one.

The ordering is correct in both ecosystems:

```
0.4.0.dev7  <  0.4.0a1  <  0.4.0b2  <  0.4.0rc1  <  0.4.0
```

No local-version segments (`+104`). PyPI rejects them outright.

### Nobody restarts, and nobody synchronises

**Each project's next version is a minor bump from wherever it already is.**
There is no coordinated 1.0.0, and no project resets its numbering.

| project | at | first release under this scheme |
|---|---|---|
| diluvium | `5.5.1_build14` | `v0.15.0` — the 14 builds are its history |
| diluvium-drt | `0.5.0` (rc series open) | continue as planned; no change |
| diluvium-lab | `0.12.0` | `v0.13.0` |
| dollup | `0.0.2` | `v0.1.0` |
| aloelite | `0.4.0` | `v0.5.0` |
| xtrshow | `1.3.0` | `v1.4.0` |
| aloeschema | `0.2.3` | `v0.3.0` |

**diluvium is the one that needs reading carefully.** "Wherever it is" means
*its own* number, not Lua's. The only diluvium-owned digit in
`5.5.1_build14` is the build counter, so fourteen builds become fourteen
minors and the next is `0.15.0`. `build14` stays the last `_build` release —
it is not rewritten.

A minor rather than a patch, because the first conforming release changes
artifact filenames and what `BUILDINFO.txt` carries. That is user-visible, so
a minor is the honest floor.

### Existing tags are never respelled

**Do not retag anything that already exists.** A published tag is a fact
other people's lockfiles and install scripts point at.

The first release *after* adopting this scheme is the first one spelled the
new way. `v0.5.0rc9` stays `v0.5.0rc9` forever; the next DRT cut is
`v0.5.0-rc.10` or `v0.5.0`. `tag_rule: exact` is a statement about the
relationship between tag and `version` field, and it holds for old entries
and new ones alike — nothing in the changelog needs rewriting either.

### Verified: do not spell a build suffix `-b<n>`

`b` is PEP 440's beta marker, and the hyphen is normalised away:

```python
>>> from packaging.version import Version
>>> Version('0.0.1-b104') == Version('0.0.1-beta104')
True
```

A build number spelled `-b104` *is* beta 104, indistinguishable from one.
`dev` is the slot that already means "a build before the release", and it
sorts below every other prerelease.

### Verified: the dot before the number is not decoration

SemVer compares dot-separated identifiers — numeric numerically, alphanumeric
lexically. A suffix with no dot is one alphanumeric blob:

```
b104     <  b2       ← build 104 sorts BEFORE build 2
dev.104  >  dev.2    ← the dot makes 104 a numeric identifier
```

Write `-dev.7`, never `-dev7`. The bug appears at build 10 and silently
misorders everything after.

### Never put an upstream version in your version string

**Rule, in two halves:** an upstream version is **never encoded** in your own
version number, and is **always recorded** as a fact.

**The test:** *can you ship a fix without the upstream moving?* If yes, and
your version has nowhere to go, the coupling is in the wrong place.

diluvium is the worked example, and the evidence is in its own tag list:

```
v5.5.1_build12
v5.5.1_build12p1      ← a patch on a build
```

There was no free digit, so one got invented mid-release. Fourteen builds of
`5.5.1` are fourteen releases that had something to say and no place to say
it, because all three numbers belong to Lua. The weld is enforced in code —
`script/changelog.py:consistency()` equates `.technoproj` with `src/lua.h`'s
`LUA_VERSION_*_N`.

DRT already resolved this and wrote the principle down in `doc/Release.md`:

> DRT versions independently of diluvium. The coupling is RECORDED, not
> required: `BUILDINFO.txt` in every release names the diluvium git revision
> this binary embeds and the dv ABI version it speaks, so "which diluvium is
> inside" is a fact you read off the release, never a tag-naming convention.

**Failing the second half is worse than failing the first.** diluvium at
least has the Lua version in the system twice. aloeschema has its upstream in
the system *zero* times — see §11. A recorded fact you can read beats a
welded number; no fact at all is a correctness bug.

### Compatibility is checked by name, never by digits

A version number is a label for humans and an ordering for tools. Anything a
consumer must *verify* — an ABI number, a bytecode format, a connector set,
an ontology release — is a named field in `BUILDINFO.txt` and in the
changelog entry, and the check compares those fields.

DRT's `doc/Release.md`: *"the digit is not the check — the connector list is,
by name."*

---

## 2. `.technoproj`

`.technoproj` holds the only version numbers a human edits. Every repository
gets one, including those without one today.

```json
{
  "TECHNO_VERSION": {
    "major": 0, "minor": 4, "patch": 0,
    "pre": { "kind": "rc", "n": 1 }
  },
  "TECHNO_CHANGELOG": { "...see §3..." },
  "TECHNO_INIT_DIRS": [".nogit", ".dump", "script"],
  "TECHNO_COPYRIGHT": "Copyright Michael Godfrey [2026] | aloecraft.org [michael@aloecraft.org]"
}
```

`pre` is `null` for a release, or `{"kind": ..., "n": ...}` for a prerelease.
This **replaces the old `build` field**, which today means three things and is
typed two ways: a PEP 440 suffix in aloelite, an off switch in xtrshow, a
build counter in diluvium.

`.technoproj` **cannot** hold the commit hash or branch — committing the file
changes the hash it claims. Those are discovered at build time (§5).

---

## 3. Shared tooling

`changelog.py` and `version.mk` become one shared copy. Today `changelog.py`
is 423 / 484 / 576 lines in diluvium / DRT / aloelite — one tool, copied
twice, then drifted.

> **Open:** the shared location is not yet decided. Until it is, treat the
> engine as vendored at `script/changelog.py` and `script/version.mk`.

The divergence turned out to be a **schema and a fact table, not logic**. So
what differs per repository is declared, in a `TECHNO_CHANGELOG` block:

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
  "mappings": [{"key": "connectors", "title": "Connectors"}],
  "tag_rule": "exact",
  "emit_json": true,
  "stamps": [
    {"file": "Cargo.toml", "find": "version\\s*=\\s*\"([^\"]+)\"",
     "spelling": "semver"}
  ]
}
```

| field | meaning |
|---|---|
| `project` | display name in the generated `CHANGELOG.md` preamble |
| `intro_extra` | repo-specific text after the Keep a Changelog line |
| `facts` | compatibility facts, in render order. A fact renders when every key it names is present; first variant matching an `id` wins |
| `mappings` | profile → list blocks (DRT's connectors and features) |
| `tag_rule` | `exact`, `prefix`, or `derive` |
| `required`, `latest_requires` | keys an entry / the `latest` entry must carry |
| `candidates`, `planned` | aloelite's candidate lists and `planned` section |
| `emit_json` | generate `changelog.json` — required for a `source: changelog` mirror |
| `stamps` | generic version-location checks: file, pattern, spelling |

**`stamps` is how a repo stops hand-typing its version in four places.** It
is not optional bookkeeping — see xtrshow in §11, which keeps the same number
in four files by hand today.

**What stays local.** Bespoke invariants do not generalise. Each repository
keeps `script/checks.py` exposing `consistency(doc, ctx) -> list[str]`, which
the engine calls if it exists:

- **diluvium** — `LUAC_FORMAT`, `LUA_VERSION_*`, the `VERSION` file
- **diluvium-drt** — the diluvium revision pinned in `Cargo.lock`
- **aloelite** — `SCHEMA_ERA` in `aloelite/db.py`
- **aloeschema** — the recorded schema.org release vs. the vendored snapshot

**The consolidation is verified.** The shared engine reproduces every
committed output byte-for-byte: diluvium's `CHANGELOG.md` (112,770 bytes) and
`changelog.json` (234,253), DRT's (173,306 / 362,887), aloelite's (33,001).
If your output changes after adopting it, the declaration is wrong — not the
engine.

---

## 4. Artifact naming

```
<project>[_<component>]_<os>_<arch>[_<libc>][_<profile>][.<ext>]
```

Fields separated by `_`, read positionally.

```
dollup_darwin_arm64
drt_linux_x86_64_musl
drt_linux_x86_64_musl_slim
drt_windows_x86_64_slim.exe
diluvium_compiler_linux_x86_64_musl
aloelite_fuse_linux_arm64_gnu
drt_wasi.wasm
drt_web.tar.gz
```

### Settled: `arm64`, not `aarch64`

Revision 1 used both. The token is **`arm64`** everywhere, on Linux and
macOS alike.

The vocabulary is ours, not the toolchain's — that is the whole point of not
using target triples — and `darwin_arm64` is already what ships. The cost is
a rename on the Linux side only, and everything in §4 is a rename anyway.

### Per-binary artifacts, plus one `complete`

A project shipping more than one binary publishes **each binary separately**,
and **one `complete` archive** carrying everything for that platform:

```
aloelite_linux_x86_64_gnu                    the CLI, bare binary
aloelite_fuse_linux_x86_64_gnu               the FUSE binary, bare
aloelite_complete_linux_x86_64_gnu.tar.gz    both, plus BUILDINFO.txt
```

`complete` is a reserved component name. Single binaries ship bare — no
archive, so `latest/` resolves to something directly executable. `complete`
is an archive because it holds more than one file.

This is a change in *what a user downloads*, not only a rename: aloelite
today ships one tarball per platform containing both binaries and nothing
else. Both forms now exist.

### The version does not go in the filename — except where a standard requires it

The release mirror materialises `latest/` as a **symlink to the tag
directory**, so this is a URL that never changes:

```
https://software.aloecraft.org/releases/diluvium-drt/latest/drt_linux_x86_64_musl
```

dollup's `install.sh` and diluvium-lab's channel default both depend on it.
Put the version in the filename and `latest/` buys nothing.

**The carve-out: Python distributions keep their version.** PEP 427 makes the
wheel filename metadata and pip's resolver parses it; PEP 625 does the same
for sdists. Renaming them breaks installation:

```
xtrshow-1.3.0-py3-none-any.whl        ← correct, do not rename
xtrshow-1.3.0.tar.gz                  ← correct, do not rename
```

So for a Python project, `latest/` carries the stable-named assets —
`BUILDINFO.txt`, `SHA256SUMS.txt`, any installer — and the wheel and sdist sit
beside them under their standard names. A consumer wanting a specific wheel
reads `releases.json`; a consumer wanting "the current checksums" uses
`latest/`. Affects xtrshow, aloelite and aloeschema.

### A fixed platform vocabulary, not target triples

- **os** — `linux`, `darwin`, `windows`
- **arch** — `x86_64`, `arm64`, `armv7`
- **libc**, where it matters — `gnu`, `musl`
- wasm targets are their own leaf: `_wasi.wasm`, `_web.tar.gz`

`static`, as in today's `linux_static_x86_64`, is a link mode rather than a
platform. `musl` says the same thing in a slot that exists.

### Renaming an artifact breaks anything that fetches it by name

The same hazard §10 describes for version strings applies to filenames, and
it is easy to miss because the breakage is at a consumer's *runtime*, not at
anyone's build.

diluvium-lab fetches diluvium's artifacts by exact constant:

```js
export const KERNEL_ARTIFACT = 'libdiluvium_wasi.wasm';
export const SWARM_ARTIFACT  = 'diluvium_swarm_wasi.wasm';
```

It resolves them against `latest/` and verifies each against the mirror's
published checksums. Rename either one and the Lab stops loading a kernel —
no build fails, no test catches it, it simply 404s in a browser.

So an artifact rename is a coordinated change:

1. Publish **both names** for one release — the new name and the old one
   beside it. Both are in `SHA256SUMS.txt`, so both stay verifiable.
2. Move every consumer you know of to the new name.
3. Drop the old name in the following release.

If you cannot enumerate the consumers, you are not ready to rename. Grep the
org before you start; `latest/`-shaped URLs are the ones that break silently.

### The name is a handle, not a specification

Everything the name cannot carry goes in `BUILDINFO.txt`, and **BUILDINFO is
what gets checked** — never the filename. DRT demonstrates the leak:
`drt_slim_windows_x86_64.exe` is the only Windows build there is and the name
cannot say so, so a workflow comment says it instead. DRT's package admission
already checks `requires.connectors` against BUILDINFO by name.

---

## 5. `BUILDINFO.txt`

Every release ships one, as a release asset, listed in `SHA256SUMS.txt`:

```
tag: v0.4.0-rc.1
version: 0.4.0-rc.1
pep440: 0.4.0rc1
commit: 3f9a1c7e2b884d05a1e6f0c9b7d4e2a8f1c33b90
branch: main
built: 2026-09-12T18:20:08Z
```

`version` is the tag body (§1). `pep440` appears only where the project
publishes to PyPI. Then this project's own compatibility facts, the same ones
its changelog entry records — DRT's per-profile connector and feature lines
are the model.

### Deriving `branch` on a tag push

A tag-triggered run carries no branch: `github.ref` is the tag. Derive it,
and record what you actually found rather than guessing:

```sh
BRANCH=$(git branch -r --contains "$SHA" --format='%(refname:lstrip=3)' \
         | grep -vx HEAD | head -1)
echo "branch: ${BRANCH:-(detached)}"
```

For a manual dispatch the input ref is authoritative and should be used
directly. A nightly or branch dev build **must** record this correctly — it
is the only thing distinguishing two dev builds cut from different branches.

---

## 6. `SHA256SUMS.txt`

**The filename is `SHA256SUMS.txt`.** Not `SHA256SUMS`.

It covers every other release asset, including `BUILDINFO.txt`, and ships as
a release asset itself:

```sh
cd release_dist && sha256sum * > SHA256SUMS.txt   # generate last
```

The release mirror verifies every mirrored file against this manifest and
refuses the tag on a mismatch. A release publishing no manifest is mirrored
*self-attested* and says so on its index page.

aloelite currently writes `SHA256SUMS` with no extension. That single missing
extension is the only reason its mirror is disabled.

---

## 7. Dev builds and nightlies

A `-dev.<n>` tag gets a specific commit into someone's hands without a
ten-minute gate and without a hash in the version string. The hash is in
`BUILDINFO.txt`, which is what lets the version stay short.

### The number is allocated from existing tags

Not stored in `.technoproj` — a counter in the tree means a commit on every
nightly, and two branches could collide.

```sh
git tag --list 'v*-dev.*' \
  | sed -n 's/.*-dev\.\([0-9][0-9]*\)$/\1/p' \
  | sort -n | tail -1 | awk '{print $1+1}'
```

`make dev-tag` prints the next free tag. The counter is global and monotonic
per repository and never reused, so `dev.105` names exactly one build
forever. Ordering holds across versions because the release segment
dominates: `0.4.0.dev104 < 0.5.0.dev105`.

### The suffix decides the rigor

A `dev` tag builds one platform and skips cross-compilation, wasm and the
slow suites. Everything else runs the full gate. Better than a dispatch
checkbox — diluvium's `run_tests` input is the only fast path any repository
has today, and what it decided is invisible once the run is over.

### Verified: a `-dev.` tag will publish to PyPI unless you stop it

This is the one that will cost you something irreversible.

```python
>>> Version('0.4.0-dev.7')
<Version('0.4.0.dev7')>
```

That is a perfectly valid PyPI version and it will upload without complaint.
Three of six repositories publish on a `v*` tag trigger. **PyPI uploads are
immutable** — you can yank a version, never replace or reuse it.

Narrow the trigger **before** the first dev tag is cut:

```yaml
on:
  push:
    tags: ['v*']
    tags-ignore: ['v*-dev.*']
```

§7 says prune old dev releases. That works on GitHub. It does not work on
PyPI, which is why the exclusion is not optional.

### Publishing and mirroring

- `prerelease: true`, always.
- Nightly: every 24 hours, and **skip if HEAD is unchanged** since the last
  dev tag, or identical builds accumulate.
- Manual: a `workflow_dispatch` with a `ref` input, so any branch can be cut.
- Prune your own old dev *GitHub* releases.
- Dev builds do **not** go in the project's main release mirror: `MIRROR_KEEP`
  is 10, so they would evict real releases, and `latest-prerelease/` would
  stop meaning "the newest release candidate". They go in a separate nightly
  mirror entry with its own retention, configured on the lk2 side — nothing
  in a project repo changes for it beyond publishing the tag.

---

## 8. Python projects

Three repositories publish to PyPI: xtrshow, aloelite, aloeschema. The rules
above apply with three adjustments, all of them already stated but collected
here because they were each discovered separately:

1. **`pyproject.toml` holds the PEP 440 spelling**, derived from the tag body
   (§1). It is the one file that does not hold the canonical form.
2. **Wheels and sdists keep their versioned filenames** (§4).
3. **The publish trigger must exclude `-dev.` tags** (§7), and PyPI is
   immutable, so this is a before-not-after change.

A fourth, for anyone adopting `stamps`: `version.mk`'s old `_sync_version`
target writes `.package.version`, which is a **Cargo** path. `pyproject.toml`
has no `[package]` table — only `[build-system]`, `[project]`, `[tool]`. The
target has therefore never worked in any Python repository; it adds a bogus
`[package]` table and leaves `[project].version` stale. It is harmless today
only because `tq` and `jyt` are not installed anywhere. Under §3 this becomes
a `stamps` entry that actually fails when the two disagree.

---

## 9. Release-workflow requirements

Two gates that several repositories lack. Both are cheap and both have
already bitten someone here.

### Publishing gates on tests

A tag must not publish anything if the suite is red. "The tests run on that
push too" is not a gate — a separate workflow triggered by the same push
races the publish and does not block it.

- **xtrshow** — `main.yml` and `publish.yml` both trigger on `push: tags: v*`
  with no `needs:` between them. A tag on red code ships to PyPI.
- **aloelite** — `release` depends on `[plan, python, native, wasm, image]`
  and no test job at all.

Either add the test job as a `needs:` dependency, or state in the workflow
why it is deliberately ungated.

### The tag must match the version in the tree

A workflow that builds from the tree and never compares the tag to the
version it built will publish the wrong number without noticing.

xtrshow's `publish.yml` does exactly this: tag `v1.4.0` while
`pyproject.toml` says `1.3.0` and PyPI quietly receives `1.3.0`. dollup has
the same class of drift already visible — `Cargo.toml` at `0.0.2`, the only
tag `v0.0.1`.

`changelog.py release-check --tag "$TAG"` is this gate. Run it in preflight.

---

## 10. Migration order, and what breaks mid-flight

### diluvium goes first

diluvium's `_buildN` counter is not only diluvium's. DRT carries it as a CLI
constant, a changelog fact and a BUILDINFO line, and **dollup checks ranges
of it**. When diluvium takes its own version line, the `N` in `5.5.1_buildN`
stops existing and becomes diluvium's version at DRT's next pin move.

**diluvium-lab is the second consumer, and it breaks on two axes.** It reads
diluvium's tags — `src/app.js` and `src/kernel/kernel.js` both reason about
`5.5.1_buildN` strings — *and* it fetches diluvium's artifacts by exact
filename (§4). A diluvium release that changes both at once changes both of
the Lab's couplings in the same moment.

The good news is that the Lab is already correct about the destination: its
`compareVersions` is a faithful SemVer implementation — numeric identifiers
compare numerically, fewer identifiers sort lower, a release outranks its
prereleases — so `0.15.0-rc.1` needs no new code. What it needs is to *keep*
parsing `5.5.1_buildN` for the releases already on the mirror.

So: **diluvium's version line is settled (§1: `v0.15.0`) before DRT moves its
pin, before dollup writes another range check, and before the Lab's artifact
constants move.** Everything else can proceed in any order.

### Version-string comparison breaks at the spelling change

Every root dollup has written pins the old spelling, and pin comparison is an
exact string match. The first binary built under the new spelling mismatches
all of them.

Two options, and **the normaliser is recommended**:

- **Normalise for one cycle.** A pure function in `drt-config` that accepts
  both `0.5.0rc9` and `0.5.0-rc.9` and compares them equal. Removable once
  no old pins remain.
- **Switch in lockstep.** Requires dollup and DRT to release together, and
  strands anyone who upgrades one and not the other.

**Why "just parse it instead" does not work here**, although it is the right
instinct. The two ecosystems disagree about whether the old spelling is even
a version:

```
PEP 440:  Version('0.5.0rc9') == Version('0.5.0-rc.9')   → True
SemVer:   0.5.0rc9                                        → no valid parse
```

SemVer's grammar requires a `-` before the prerelease, so a semver parser
*rejects* `0.5.0rc9` rather than comparing it. dollup's pins are Rust-side,
so parsing cannot rescue them and the normaliser is genuinely required.

The inverse is also useful: a **Python** consumer comparing these needs no
normaliser at all, because PEP 440 already treats the two spellings as the
same version. So this migration cost lands on the Rust side only. Parse where
parsing works; normalise only where it does not.

### `.technoproj` and `version.mk` land in the same commit

`version.mk` reads `.TECHNO_VERSION.build`. Drop `build` for `pre` on its own
and `make echo` starts printing `0.2.3 build ` with an empty tail. Nothing
else in any repository reads that field, so this is the whole of the
breakage — but the two files must move together.

---

## 11. Per-repository checklist

### aloeschema — **do this one first**

The only item in this document that is a correctness problem for *users*
rather than a process problem for us.

`src/data/schemaorg_current.py` is a 1.5 MB vendored snapshot of the
schema.org ontology, and **nothing anywhere names which release it is** — the
top level is only `@context` and `@graph`, and none of its 3,187 nodes
carries a version marker. Meanwhile `load_schema_org(fetch=True)` pulls
`version/latest` live at runtime.

So two callers on the same aloeschema version can be holding different
ontologies with no way to tell them apart. §1's rule has two halves;
aloeschema passes "don't encode" by accident and fails "do record" entirely.

- [ ] **Record a `schemaorg_release` fact** in the changelog entry and in
      `BUILDINFO.txt`
- [ ] **`script/checks.py` invariant** tying the recorded release to the
      vendored snapshot
- [ ] Decide what `fetch=True` should do when the live release differs from
      the recorded one — at minimum it should be visible, not silent
- [ ] `.technoproj`, the shared engine, and a `CHANGELOG.yaml` (greenfield —
      no changelog exists, so §3's byte-for-byte bar does not apply)
- [ ] `stamps` for `pyproject.toml` — `_sync_version` never worked here
      either (§8), the two agree only because someone typed it twice
- [ ] Publish trigger: exclude `-dev.` (§7), and decide the test gate (§9)
- [ ] **Decide: mirrored or PyPI-only?** Zero GitHub releases have ever been
      cut. If PyPI-only, say so in `mirrors.json` and close the entry rather
      than carrying it as "waiting on the repo".

### diluvium

**Settled (§1): the next release is `v0.15.0`.** The fourteen `_build`
releases are diluvium's own history, so they become fourteen minors and the
counter simply carries on. `build14` stays the last `_build` release and is
not rewritten. No synchronised 1.0.0.

This repo is first in the order (§10) — DRT's pin, dollup's range checks and
the Lab's constants all wait on it.

- [ ] Its own version line. `lua_base` and `bytecode_format` stay recorded
      facts — they already are, and the runtime already models the split
      (`_DILUVIUM.version` is separate from `.lua` and `.bytecode_format`)
- [ ] Unweld `consistency()` — it currently equates `.technoproj` with
      `src/lua.h`'s `LUA_VERSION_*_N`
- [ ] `.technoproj`: `build: "14"` → `pre`
- [ ] `VERSION` file: reconcile or retire
- [ ] Artifact names: `_linux_static_x86_64` → `_linux_x86_64_musl`, for
      `diluvium`, `_compiler`, `_host`, `_rest_plugin`
- [ ] Adopt the shared engine; bespoke checks → `script/checks.py`
- [ ] Keep `tag_rule: "prefix"` — upstream Lua's tags share this namespace

Already conforms: `changelog.json`, `SHA256SUMS.txt`, a fast path
(`run_tests`), and half of §1 — the facts are recorded already.

### diluvium-drt

- [ ] Gains a `.technoproj`
- [ ] New tags use `-rc.N`; **`v0.5.0rc9` and everything before it stay as
      they are** (§1)
- [ ] **The pin normaliser** (§10) — a pure function in `drt-config`
      accepting both spellings for one cycle
- [ ] Artifact names: profile moves last — `drt_slim_linux_static_x86_64` →
      `drt_linux_x86_64_musl_slim`; `drt_slim_windows_x86_64.exe` →
      `drt_windows_x86_64_slim.exe`
- [ ] Adopt the shared engine; `Cargo.lock` pin check → `script/checks.py`
- [ ] A `dev` fast path — `publish` currently needs test, build,
      build-wasip2, build-web, build-windows and smoke-windows
- [ ] `branch` derivation for tag pushes (§5)

Already conforms: `changelog.json`, `SHA256SUMS.txt`, `BUILDINFO.txt`, and it
is the reference implementation for §1's recorded-coupling rule.

### diluvium-lab

The only JavaScript project on the list, and `private: true` — it publishes
to no registry, so §8 does not apply and there is no npm equivalent to worry
about. It is served as a static site through the lk_web site contract.

**It already got §1 right on its own.** `src/version.js` carries the rule
this document argues for, written before this document existed:

```js
// Pre-release suffixes are semver's: `0.2.0-rc.1`. Note the hyphen and
// the dot -- `0.2.0_rc1` is not semver and does not sort.
export const LAB_VERSION = '0.12.0';
```

Its `compareVersions` is a faithful SemVer implementation and needs no change
for the new scheme.

- [ ] **Version is hand-typed in two places** — `package.json` and
      `src/version.js`'s `LAB_VERSION`, both `0.12.0`. `stamps` (§3) covers
      exactly this.
- [ ] `.technoproj` (it has none) and the shared engine; `CHANGELOG.yaml` is
      greenfield here
- [ ] Next release is `v0.13.0` (§1)
- [ ] **Keep parsing `5.5.1_buildN`** after diluvium moves — those releases
      stay on the mirror and the Lab still has to rank them (§10)
- [ ] **`KERNEL_ARTIFACT` / `SWARM_ARTIFACT` move when diluvium renames its
      artifacts** (§4). Coordinate: diluvium publishes both names for one
      release, the Lab moves, diluvium drops the old name.
- [ ] Decide: mirrored or not? Its `mirrors.json` entry is disabled pending
      "a proper DRT wasm release to build against". There are no tags and no
      release workflow, only `ci.yml` — so as with xtrshow and aloeschema,
      either publish releases or close the entry.

Already conforms: SemVer-correct comparison, and `DEFAULT_MIRROR` already
points at `software.aloecraft.org/releases/diluvium/` — which means step 1 of
lk2's `/release/` retirement is done on this side and the lk2 half can
proceed.

### dollup

- [ ] **Fix the drift:** `Cargo.toml` says `0.0.2`, the only tag is `v0.0.1`
- [ ] Gains `.technoproj`, `version.mk`, `CHANGELOG.yaml` and the engine
- [ ] **`release.yml` sets no `prerelease` flag and uses
      `generate_release_notes: true`.** Its mirror is `source: github`, where
      GitHub's prerelease flag decides stable — so every dollup release
      publishes as stable with an autogenerated commit list. Derive both from
      the changelog.
- [ ] Artifact names: `dollup_linux_static_x86_64` → `dollup_linux_x86_64_musl`
- [ ] Coordinate the diluvium-build range checks with §10's ordering

Already conforms: `SHA256SUMS.txt`, `BUILDINFO.txt`, `install.sh` as an asset.

### aloelite

- [ ] **`SHA256SUMS` → `SHA256SUMS.txt`.** This alone unblocks its mirror.
- [ ] Artifact names: per-binary plus `complete` (§4); drop the version and
      the Rust triples
- [ ] **`v0.4.0`:** retag on `386c016` under the old spelling — it is already
      written `v0.4.0` and the changelog claims that name — and make `0.5.0`
      the first release under the new scheme. One conforming release beats a
      half-migrated one, and it means `0.4.0` actually ships.
- [ ] `.technoproj`: `build: 0` → `pre: null`
- [ ] Adopt the shared engine; `SCHEMA_ERA` → `script/checks.py`
- [ ] `emit_json: true` if it should be mirrored from its changelog rather
      than from GitHub — it generates no `changelog.json` today
- [ ] **Gate `release` on tests** (§9) — it depends on no test job
- [ ] Publish trigger: exclude `-dev.` (§7)

Already conforms: `CHANGELOG.yaml`, a working `pep440_to_semver`, release
notes from the changelog.

### xtrshow

Cheapest repo on the list. Every tag is a plain `vX.Y.Z`, no prerelease has
ever been cut, and there are no artifacts — so `-rc.N` / `-dev.N` is purely
additive and §4 has no legacy to break. The first release workflow can be
born conforming.

- [ ] **The version is hand-typed in four places** — `.technoproj`,
      `pyproject.toml:7`, `xtrshow/__init__.py:9`, `web/vendor/VERSION`. Both
      release-prep commits bump all four together. `stamps` (§3) is for
      exactly this.
- [ ] **Delete `__version__`** — nothing imports it; both CLIs call
      `get_version()`, which reads installed distribution metadata, so it can
      only ever disagree. Keep `web/vendor/VERSION`: under Pyodide the
      package is not pip-installed and `get_version()` returns
      `"unknown (not installed)"`, which is why `web/assets/demo.js` fetches
      that file.
- [ ] **Correction to revision 1:** `version.mk` here does *not* conform. It
      is Cargo-shaped (`_sync_version` writes `.package.version`, §8),
      carries `__VERSION_FULL := 1.3.0 build 0` — a fifth spelling the new
      scheme deletes — and has no `dev-tag` target.
- [ ] **Gate PyPI publishing on tests** (§9) and add a tag ↔ version gate
- [ ] Publish trigger: exclude `-dev.` (§7)
- [ ] `.technoproj`: `build: 0` → `pre: null`; gains `CHANGELOG.yaml`
- [ ] **Decide: mirrored or PyPI-only?** Ten tags exist and zero GitHub
      releases — a `source: github` mirror would find nothing to read even if
      enabled today. Recommendation: publish GitHub releases carrying
      `BUILDINFO.txt`, `SHA256SUMS.txt` and the sdist/wheel, and keep the
      entry open. ~20 lines added to `publish.yml`, and it invents no
      platform artifacts for a pure-Python package.

---

## 12. Non-negotiable

Everything else is a default you can argue with. These break something
concrete:

1. `-dev.<n>` with the dot, never `-b<n>` or `-dev<n>` — **§1, verified**
2. Existing tags are never respelled, and no project restarts or
   synchronises its numbering — **§1**
3. No version in artifact filenames, except wheels and sdists — **§4**
4. An artifact rename publishes both names for one release — **§4**
5. `SHA256SUMS.txt`, with the extension — **§6**
6. Upstream versions recorded, never encoded — and *recorded* is the half
   that matters — **§1, §11 aloeschema**
7. PyPI publish triggers exclude `-dev.` tags, before the first one is cut —
   **§7, verified, irreversible**
8. Dev builds flagged `prerelease: true` and kept out of the main mirror —
   **§7**
