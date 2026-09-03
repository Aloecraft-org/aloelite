# Releasing

How a version of Aloelite goes out, end to end. Everything downstream of
`CHANGELOG.yaml` is derived; the tag is the only manual push.

## Where the version lives

| file | field | spelling |
|---|---|---|
| `pyproject.toml` | `version` | PEP 440: `0.4.0rc1`, then `0.4.0` |
| `.technoproj` | `TECHNO_VERSION` | `major`/`minor`/`patch`, `build: "rc1"` or `0` |
| `rust/Cargo.toml` | `[workspace.package] version` | SemVer: `0.4.0-rc.1`, then `0.4.0` |
| `CHANGELOG.yaml` | the newest `releases` entry | `version: "0.4.0"`, with candidates listed under it |

`script/changelog.py consistency` holds the four together and runs in CI's
`lint` job, so they cannot drift quietly: the newest entry's `X.Y.Z` must
match `.technoproj`, `pyproject.toml` must be that version or a candidate of
it (and the newest candidate listed, if any), and `rust/Cargo.toml` must
spell the same version SemVer's way.

## A release candidate

1. Stamp the candidate: `pyproject.toml` to `X.Y.ZrcN`, `.technoproj` build
   to `"rcN"`, `rust/Cargo.toml` to `X.Y.Z-rc.N`.
2. In `CHANGELOG.yaml`, under the `X.Y.Z` entry (which stays
   `status: unreleased`, `stable: false`), add the candidate:
   ```yaml
   candidates:
     - version: "X.Y.ZrcN"
       date: "YYYY-MM-DD"
   ```
3. `script/changelog.py generate`, then `consistency` and
   `release-check --tag vX.Y.ZrcN --publish`; commit `CHANGELOG.md` with it.
4. Tag and push: `git tag -a vX.Y.ZrcN -m "vX.Y.ZrcN" && git push origin vX.Y.ZrcN`.

## The final

1. Stamp `X.Y.Z` everywhere (Cargo `X.Y.Z`, `.technoproj` build `0`).
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

`release.yml` publishes, for a version `V`:

| asset | contents |
|---|---|
| `aloelite-V-py3-none-any.whl`, `aloelite-V.tar.gz` | the Python package |
| `aloelite-V-x86_64-unknown-linux-gnu.tar.gz` | `aloelite`, `aloelite-fuse` |
| `aloelite-V-aarch64-unknown-linux-gnu.tar.gz` | `aloelite`, `aloelite-fuse` |
| `aloelite-V-x86_64-unknown-linux-musl.tar.gz` | `aloelite`, `aloelite-fuse`, static |
| `aloelite-V-aarch64-apple-darwin.tar.gz`, `aloelite-V-x86_64-apple-darwin.tar.gz` | `aloelite` |
| `aloelite-V-x86_64-pc-windows-msvc.zip` | `aloelite.exe` |
| `aloelite-V-wasm32-wasip2.wasm` | the CLI as a WASI component (`wasmtime run --dir=.::/work aloelite.wasm -f /work/x.fs ls /`) |
| `aloelite-wasm-V.tar.gz` | the browser package: ES module, `.wasm`, `.d.ts`, README |
| `SHA256SUMS` | over all of the above |
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
