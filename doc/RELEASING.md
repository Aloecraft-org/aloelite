# Releasing

How a version of Aloelite goes out, end to end. Everything downstream of
`CHANGELOG.yaml` is derived; the tag is the only manual push.

> [`ALIGNMENT.md`](/doc/ALIGNMENT.md) is the org-wide standard every
> Aloecraft project is moving to; it is shared verbatim across repositories
> and is not edited here. What this document describes now conforms to its
> §1, §2 and §4 — the version scheme, `.technoproj`'s `pre`, and artifact
> names with no version in them — and to §6's `SHA256SUMS.txt`. Its last open
> question, whether releasing gates on tests, is answered below: it does.
> Still owed from its §8 checklist: the shared changelog engine with
> `script/checks.py`, `emit_json` if aloelite is to be mirrored from its
> changelog, §5's `BUILDINFO.txt`, and §7's `-dev.<n>` builds.

## Where the version lives

| file | field | spelling |
|---|---|---|
| `pyproject.toml` | `version` | PEP 440: `0.4.0rc1`, then `0.4.0` |
| `.technoproj` | `TECHNO_VERSION` | `major`/`minor`/`patch`, plus `pre: null` or `{"kind": "rc", "n": 1}` |
| `rust/Cargo.toml` | `[workspace.package] version` | SemVer: `0.4.0-rc.1`, then `0.4.0` |
| `CHANGELOG.yaml` | the newest `releases` entry | `version: "0.4.0"`, with candidates listed under it |

`script/changelog.py consistency` holds the four together and runs in CI's
`lint` job, so they cannot drift quietly: the newest entry's `X.Y.Z` must
match `.technoproj`, `.technoproj`'s `pre` must spell what `pyproject.toml`
spells, `pyproject.toml` must be that version or a candidate of it (and the
newest candidate listed, if any), and `rust/Cargo.toml` must spell the same
version SemVer's way.

`make echo` prints all three spellings, derived from `.technoproj` alone by
`script/version.mk` — there is no second definition of any of them:

```
VERSION: 0.5.0rc1      # PEP 440, what pyproject and PyPI carry
SEMVER:  0.5.0-rc.1    # what rust/Cargo.toml carries
TAG:     v0.5.0-rc.1   # what you push
```

## A release candidate

1. Stamp the candidate: `make pre_set KIND=rc N=<n>`, then `pyproject.toml`
   to `X.Y.ZrcN` and `rust/Cargo.toml` to `X.Y.Z-rc.N` — the two spellings
   `make echo` just printed.
2. In `CHANGELOG.yaml`, under the `X.Y.Z` entry (which stays
   `status: unreleased`, `stable: false`), add the candidate:
   ```yaml
   candidates:
     - version: "X.Y.ZrcN"
       date: "YYYY-MM-DD"
   ```
3. `script/changelog.py generate`, then `consistency` and
   `release-check --tag vX.Y.ZrcN --publish`; commit `CHANGELOG.md` with it.
4. Tag and push: `git tag -a vX.Y.Z-rc.N -m "vX.Y.Z-rc.N" && git push origin vX.Y.Z-rc.N`.
   The tag is `make echo`'s `TAG` line. Candidates tagged the old way
   (`vX.Y.ZrcN`) still resolve, so an old release can be re-run; new ones use
   the spelling above (`ALIGNMENT.md` §1 — the dot before the number is what
   makes candidate 10 sort after candidate 2).

## The final

1. Stamp `X.Y.Z` everywhere: `make pre_clear`, then `pyproject.toml` and
   `rust/Cargo.toml` to `X.Y.Z`.
2. The entry: `status: released`, a `date`, `stable: true`, and move
   `latest: true` onto it (off the previous release). Keep `candidates`; they
   are the record of what preceded it.
3. `generate`, `consistency`, `release-check --tag vX.Y.Z --publish`; commit.
4. Tag and push `vX.Y.Z` as above.

## What a tag triggers

Three workflows run on `v*`, independently:

- **`main.yml`** — the CI matrix, on the tagged commit.
- **`publish.yml`** — PyPI, by trusted publishing. Unchanged by the release
  work; a candidate is a PyPI pre-release, which `pip` skips unless asked.
- **`release.yml`** — the GitHub release and the image. It refuses a tag the
  changelog does not claim, renders the release body from the entry, and
  derives `prerelease` (a candidate always; a final from `stable`).

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

`release.yml` publishes, for a version `V`:

Asset names carry no version, by `ALIGNMENT.md` §4: the release mirror
materialises `latest/` as a symlink to the tag directory, so a versioned
filename would mean no download URL that stays put. The platform vocabulary
is `<os>_<arch>[_<libc>]`, not Rust triples, and a profile token comes last.

| asset | contents |
|---|---|
| `aloelite-V-py3-none-any.whl`, `aloelite-V.tar.gz` | the Python package — the one pair that keeps a version, because PyPI mandates the name |
| `aloelite_linux_x86_64_gnu`, `_aarch64_gnu`, `_x86_64_musl` | the `aloelite` CLI, one bare binary each |
| `aloelite_fuse_linux_x86_64_gnu`, `_aarch64_gnu`, `_x86_64_musl` | the FUSE daemon, same three |
| `aloelite_darwin_aarch64`, `aloelite_darwin_x86_64` | the CLI on macOS |
| `aloelite_windows_x86_64.exe` | the CLI on Windows |
| `aloelite_<platform>_complete.tar.gz` (`.zip` on Windows) | every binary built for that platform, plus the README |
| `aloelite_wasi.wasm` | the CLI as a WASI component (`wasmtime run --dir=.::/work aloelite_wasi.wasm -f /work/x.fs ls /`) |
| `aloelite_web.tar.gz` | the browser package: ES module, `.wasm`, `.d.ts`, README |
| `SHA256SUMS.txt` | over all of the above |
| `ghcr.io/aloecraft-org/aloelite:V` | the manager image, amd64 and arm64; `:latest` too for a stable release |

Docker Hub (`aloecraft/aloelite`) is not part of this; the Makefile's
`push_container` remains the manual route. A GHCR package created by a
workflow may need its visibility set to public once, in the package's
settings, before anonymous pulls work.

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
