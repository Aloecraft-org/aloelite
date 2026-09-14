# Releasing

How a version of Aloelite goes out, end to end. Everything downstream of
`CHANGELOG.yaml` is derived; the tag is the only manual push.

> [`ALIGNMENT.md`](/doc/ALIGNMENT.md) is the org-wide standard every
> Aloecraft project is moving to. The canonical copy lives in
> [Aloecraft-org/technoproj](https://github.com/Aloecraft-org/technoproj);
> the copy here is byte-identical to it and is not edited, so it can be
> diffed against upstream.
>
> What this document describes conforms to it throughout: §1, §2 and §4 — the
> version scheme, `.technoproj`'s `pre`, and artifact names with no version in
> them — §5's `BUILDINFO.txt`, §6's `SHA256SUMS.txt`, §7's dev builds (which
> `nightly.yml` cuts and `publish.yml` keeps off PyPI), §8's Python rules and
> §9's three gates. Its §3 is the reason the release tooling is
> [technoproj](https://github.com/Aloecraft-org/technoproj), installed in CI
> and pinned rather than copied into this tree, and aloelite is mirrored from
> its changelog, so `emit_json` is on and `changelog.json` is committed.

## The tooling

`technoproj-changelog` renders and checks `CHANGELOG.yaml`; `technoproj
sync` places `script/version.mk` and `technoproj check` fails when that copy
has drifted. What differs per repository is declared rather than forked:
`.technoproj`'s `TECHNO_CHANGELOG` block holds this project's facts,
required keys and version stamps, and `script/checks.py` holds the one
invariant that does not generalise — `SCHEMA_ERA` in `aloelite/db.py`
against the era the newest entry claims.

`make` has to read `version.mk` with no network and no virtualenv, which is
why that one file is copied into the tree instead of imported.

`generate` writes two files and both are committed: `CHANGELOG.md`, and
`changelog.json` for the release mirror — which sources aloelite from its
changelog rather than from GitHub's release list, on a host running a
stdlib-only Python with no build step. Each entry in the JSON carries its
notes already rendered, and `mirror_tags` is the list of tags the mirror
should carry, newest first. Neither file is edited by hand;
`technoproj-changelog check` runs in CI and fails when either has drifted
from the YAML.

## Where the version lives

| file | field | spelling |
|---|---|---|
| `pyproject.toml` | `version` | **derived** — PEP 440: `0.5.0rc1`, then `0.5.0` |
| `.technoproj` | `TECHNO_VERSION` | `major`/`minor`/`patch`, plus `pre: null` or `{"kind": "rc", "n": 1}` |
| `rust/Cargo.toml` | `[workspace.package] version` | the tag body: `0.5.0-rc.1`, then `0.5.0` |
| `CHANGELOG.yaml` | the newest `releases` entry | the tag body: `version: "0.5.0"`, with candidates listed under it |

The tag is canonical and the **tag body** — the tag without its `v` — is what
`CHANGELOG.yaml` and `rust/Cargo.toml` hold. PEP 440 is a *derived* spelling
that exists only in `pyproject.toml` and on PyPI (`ALIGNMENT.md` §1).

`technoproj-changelog consistency` holds the four together and runs in CI's
`lint` job, so they cannot drift quietly: the newest entry's `X.Y.Z` must
match `.technoproj`, `.technoproj`'s `pre` must spell what `pyproject.toml`
spells, `pyproject.toml` must be that version or a candidate of it (and the
newest candidate listed, if any), and `rust/Cargo.toml` must spell the same
version SemVer's way.

`make version` prints every spelling, derived from `.technoproj` alone by
`script/version.mk` — there is no second definition of any of them:

```
version: 0.5.0 rc 1
tag:     v0.5.0-rc.1   # what you push
pep440:  0.5.0rc1      # what pyproject and PyPI carry
semver:  0.5.0-rc.1    # what rust/Cargo.toml carries
```

## A release candidate

1. Stamp the candidate: `make set_pre KIND=rc N=<n>`, then `pyproject.toml`
   and `rust/Cargo.toml` to the `pep440:` and `semver:` lines `make version`
   just printed.
2. In `CHANGELOG.yaml`, under the `X.Y.Z` entry (which stays
   `status: unreleased`, `stable: false`), add the candidate:
   ```yaml
   candidates:
     - version: "X.Y.Z-rc.N"
       date: "YYYY-MM-DD"
   ```

   The tag body, not PEP 440. Candidates written the old way (`X.Y.ZrcN`)
   still resolve and are not rewritten.
3. `technoproj-changelog generate`, then `consistency` and
   `release-check --tag vX.Y.Z-rc.N --publish`; commit `CHANGELOG.md` and
   `changelog.json` with it. A candidate is not mirrored, so it takes no
   `mirror` flag.
4. Tag and push: `git tag -a vX.Y.Z-rc.N -m "vX.Y.Z-rc.N" && git push origin vX.Y.Z-rc.N`.
   The tag is `make version`'s `tag:` line. Candidates tagged the old way
   (`vX.Y.ZrcN`) still resolve, so an old release can be re-run; new ones use
   the spelling above (`ALIGNMENT.md` §1 — the dot before the number is what
   makes candidate 10 sort after candidate 2).

## The final

1. Stamp `X.Y.Z` everywhere: `make clear_pre`, then `pyproject.toml` and
   `rust/Cargo.toml` to `X.Y.Z`.
2. The entry: `status: released`, a `date`, `stable: true`, `mirror: true`,
   and move `latest: true` onto it (off the previous release). Keep
   `candidates`; they are the record of what preceded it. `validate` refuses
   a `latest` entry that has not answered the mirror question, and refuses
   `mirror: true` on anything not yet released.
3. `technoproj-changelog generate`, `consistency`,
   `release-check --tag vX.Y.Z --publish`; commit `CHANGELOG.md` and
   `changelog.json` with it.
4. Tag and push `vX.Y.Z` as above.

## What a tag triggers

Three workflows run on `v*`, independently:

- **`main.yml`** — the CI matrix, on the tagged commit. It ignores
  `v*-dev.*`: skipping the slow suites is what that suffix buys.
- **`publish.yml`** — PyPI, by trusted publishing. **Its filename is part of
  the grant** — owner, repository, workflow filename, environment — so
  renaming it revokes the publisher, and a tag's OIDC claim resolves from the
  commit the tag points at, which means the rename cannot be repaired for a
  tag that already exists (`ALIGNMENT.md` §8; aloeschema spent a version
  finding this out). The file says so at the top. A candidate is a PyPI
  pre-release, which `pip` skips unless asked. It ignores `v*-dev.*` tags:
  `0.5.0-dev.7` is a valid PEP 440 version and would upload without
  complaint, and a PyPI upload can be yanked but never replaced or reused
  (`ALIGNMENT.md` §7).
- **`release.yml`** — the GitHub release and the image. It refuses a tag the
  changelog does not claim, renders the release body from the entry, and
  derives `prerelease` (a candidate always; a final from `stable`). A
  `v*-dev.*` tag takes a different route through it — see Dev builds.

Both `!v*-dev.*` exclusions are a `!` pattern **second in the same `tags`
list**, never a `tags-ignore` beside `tags`: Actions rejects both filters on
one event *at load time*, and a workflow that does not parse does not run at
all — so the wrong spelling does not narrow the trigger, it silently switches
the workflow off with no failed run to notice.

### Nothing ships untested

`release.yml`'s builds run in parallel with `main.yml`'s matrix, which is two
independent workflow runs with nothing connecting them. Its `gate` job is
that connection: it waits for a **successful `main.yml` run on the same
commit**, and the two jobs that publish anything — `image`, which pushes to
GHCR itself, and `release` — wait for it. The build matrix does not, so the
gate costs nothing on the slow half.

Usually it costs nothing at all. A commit is pushed to `main` and tested
before it is tagged, so a green run on that SHA already exists and the gate
returns at once; a run from the push and a run from the tag are the same
commit and the same matrix, so either satisfies it. It blocks only when a tag
lands on a commit whose tests are still running, and fails if they finish
without a success, if no run exists for that commit within forty minutes, or
if the only runs were cancelled.

Two exemptions, both deliberate: a dry run publishes nothing so the gate
no-ops, and a `-dev.<n>` build skips it, which is what that suffix is for
(`ALIGNMENT.md` §7). A re-run dispatch with `publish` on is *not* exempt — it
finds the original tag push's run and passes on it.

### And the wheel that ships is the wheel that was tested

A green suite is not the same claim as a working artifact, and `ALIGNMENT.md`
§9's third gate is the difference. CI installs this package with `pip install
-e .`, which resolves it straight out of the source tree — so a packaging
mistake is invisible to every other gate in this document. The `python` job
therefore installs **the wheel it just built** into a clean virtualenv and
imports through it:

```
aloelite.cli, manager.engine, manager.ui   the subpackages
aloelite --version                         the console entry point
```

The subpackages are the whole point. `aloeschema` published five wheels whose
`aloeschema.data` was simply absent — `packages.find`'s `include` named the
parent and setuptools does not imply children — while its tests passed
against the source tree throughout.

This repository is not carrying that bug: its `include` is
`["aloelite*", "manager*"]`, and the globs match `manager.engine` and
`manager.ui`. That was confirmed against the **published** 0.4.0 wheel from
PyPI, not just against a local build. The gate exists so that it stays true
without anyone checking again, and it was run in both directions before being
trusted — against the real wheel, which passes, and against one built with
the `include` globs removed, which it rejects with `ModuleNotFoundError: No
module named 'manager.engine'`. §9's own rule: a gate that has only ever been
seen to pass is not yet a gate.

`release.yml` publishes, for a version `V`:

Asset names carry no version, by `ALIGNMENT.md` §4: the release mirror
materialises `latest/` as a symlink to the tag directory, so a versioned
filename would mean no download URL that stays put. The platform vocabulary
is `<os>_<arch>[_<libc>]`, not Rust triples, and a profile token comes last.

| asset | contents |
|---|---|
| `aloelite-V-py3-none-any.whl`, `aloelite-V.tar.gz` | the Python package — the one pair that keeps a version, because PyPI mandates the name |
| `aloelite_linux_x86_64_gnu`, `_arm64_gnu`, `_x86_64_musl` | the `aloelite` CLI, one bare binary each |
| `aloelite_fuse_linux_x86_64_gnu`, `_arm64_gnu`, `_x86_64_musl` | the FUSE daemon, same three |
| `aloelite_darwin_arm64`, `aloelite_darwin_x86_64` | the CLI on macOS |
| `aloelite_windows_x86_64.exe` | the CLI on Windows |
| `aloelite_complete_<platform>.tar.gz` (`.zip` on Windows) | every binary built for that platform, plus `BUILDINFO.txt` |
| `BUILDINFO.txt` | tag, version, PEP 440 spelling, commit, branch, build time, and the schema era this build writes |
| `aloelite_wasi.wasm` | the CLI as a WASI component (`wasmtime run --dir=.::/work aloelite_wasi.wasm -f /work/x.fs ls /`) |
| `aloelite_web.tar.gz` | the browser package: ES module, `.wasm`, `.d.ts`, README |
| `SHA256SUMS.txt` | over all of the above |
| `ghcr.io/aloecraft-org/aloelite:V` | the manager image, amd64 and arm64; `:latest` too for a stable release |

Docker Hub (`aloecraft/aloelite`) is not part of this; the Makefile's
`push_container` remains the manual route. A GHCR package created by a
workflow may need its visibility set to public once, in the package's
settings, before anonymous pulls work.

## Dev builds

A `-dev.<n>` build gets one commit into someone's hands without a ten-minute
gate and without a hash in the version string — the hash is in
`BUILDINFO.txt`, which is what lets the version stay short (`ALIGNMENT.md`
§7). It is not a
release: it has no `CHANGELOG.yaml` entry and never will, it never reaches
PyPI, it is not mirrored, and it is pruned once newer ones exist.

`nightly.yml` cuts them — every night at 06:00 UTC, and on dispatch with a
`ref` for any branch. It **skips when `HEAD` has not moved** since the last
dev tag, so identical builds do not accumulate; `force` overrides that.

```
make dev-tag        # v0.5.0-dev.1 — the number that would be allocated next
```

**The number is global and never reused.** It is allocated from the tags
that exist, not from a counter in the tree, so a nightly needs no commit and
two branches cannot collide. It is monotonic across versions —
`0.4.0.dev104` then `0.5.0.dev105` — and because `nightly.yml` prunes old dev
*releases* while leaving their *tags*, a number names exactly one build
forever.

**What a dev tag does differently**, all of it keyed off the `-dev.` in the
tag:

| | a release tag | a dev tag |
|---|---|---|
| `CHANGELOG.yaml` entry | required; `release-check` refuses a tag without one | none, ever; `plan` skips the check |
| release body | rendered from the entry | generated, with `BUILDINFO.txt` in it |
| platforms | all six | `linux_x86_64_gnu` alone, no cross-compilation |
| wasm | both leaves | skipped |
| image | amd64 + arm64 | amd64 |
| `CI/CD` | runs on the tag, and `release.yml` waits for it | does not run, and is not waited for |
| PyPI | published | excluded by `publish.yml` |
| `prerelease` | from `stable` | always true |
| version | the tree's | the tag's: `0.5.0-dev.1`, PEP 440 `0.5.0.dev1` |

That last row is the one with teeth. A dev build is the single case where
the tag is more authoritative than the tree, because the tree has no way to
know which commit was cut — so `plan` derives the PEP 440 spelling from the
tag and the `python` job stamps it into `pyproject.toml` before building.
Without that the wheel would be named `aloelite-0.5.0-py3-none-any.whl` and
claim to be the release it is not.

**The tag is pushed by CI, then `release.yml` is dispatched for it.** Not
because a dispatch is nicer, but because a ref pushed with `GITHUB_TOKEN`
starts no workflow run — GitHub's recursion guard, which cannot be turned
off. A dispatch is not subject to it.

## Proving the matrix before a tag exists

Two ways, both building every artifact exactly as a tag would and keeping
it on the run, where the `manifest` job writes the asset list with sizes to
the run summary; nothing is published:

- **Edit the pipeline.** A push that changes `.github/workflows/release.yml`
  on any branch dry-runs it, so a change to the release pipeline tests
  itself. (`paths` filters are not applied to tag pushes, so a tag always
  runs.)
- **Dispatch it.** `release.yml` from any ref with `publish` off. GitHub
  accepts a dispatch by file name once the workflow exists on the default
  branch, and by numeric workflow id (348935098 for this repository, the
  number in its Actions API URL) as soon as it has run once on any branch,
  which the self-test push above provides. On a feature branch that has
  not merged yet, dispatch by id.

## Re-running a release

A tag pushes once; when its run dies of infrastructure, dispatch
`release.yml` (by id while it is not yet on the default branch) with the tag
as `ref` and `publish` on. The release is updated in place and its assets
replaced, so no new tag is needed. `publish.yml` has the same escape hatch
for PyPI.

**A tag push runs the workflow file as it exists at that tag**, not the one
on `main`. A dispatch is the other way round — the workflow comes from the
ref it is dispatched on, and `ref` only says what to check out — which is
what makes the re-run above able to rebuild an old tag with today's
pipeline.

That only works back as far as the tree the pipeline can read, though. The
engine needs `.technoproj`'s `TECHNO_CHANGELOG` block, so a tag from before
it existed — `v0.4.0` and earlier — cannot be re-run through this pipeline
at all: `release-check` exits with `TECHNO_CHANGELOG.project is required`
before anything is built. **`v0.4.0`'s assets are therefore the old scheme**
— versioned names, Rust triples, `SHA256SUMS` with no extension — and
permanently so. `ALIGNMENT.md` §11 asks for that rather than works around
it; 0.5.0 is the first release under the naming above.
