# Log

Append only. One entry per run: plan, what was done, what was learned,
what is next.

## 2026-10-03 fill the lockstep scaffold

Plan: fill what the owner's answers already settle (owner, sources) as a
PROPOSAL; leave goal.md and the I1 iteration for the owner.

Done: owner `Michael Godfrey` in authority.yaml; sources.yaml points at this
repo's CHANGELOG.yaml as the facts source, README.md and doc/ as docs.

Next: the owner writes goal.md and I1 in roadmap.md; then `technoproj
lockstep check` passes and goes in CI. Step 4 of the migration (working
notes out of doc/) is its own pull request.

## 2026-11-01 embeddable file explorer

Plan: the owner asked directly for a file explorer a web page embeds with
one import and one call. Outside I0 Seed, but an owner request, so one
branch, one pull request. Build it over the Mount API only, so it needs
nothing new from the crate; test both handle shapes (Fs directly, Worker
with OPFS) in Chromium.

Done: `rust/aloelite-wasm/explorer/` (module, demo page, demo Worker),
a section in the crate README, a 0.5.0 `added` entry, `rust/pkg/` ignored.
Browser test (Playwright, scratch only): browse, preview text, image and
binary, download, upload, edit and save, rename, new folder, delete, in
both modes, and persistence across a reload in OPFS mode.

Learned: a module Worker drops messages posted before `serve` runs; the
README's Worker example has this race when the page calls right away (the
demo Worker queues early messages). A durable rw mount left by a reloaded
page meets the next as `mount_conflict`, so the explorer mounts with a
renewed ttl and the demo stacks with `allow_overlap`.

Next: owner reviews the pull request; decide whether the README Worker
example should queue early messages too.

## 2026-11-02 install scripts, after diluvium-drt's

Plan: the owner asked in the project thread for install scripts like
diluvium-drt's. That is outside I0, which is still active; the owner's
direct ask is the authority for it, and nothing here touches lockstep.
One pull request on claude/project-thread-obz8nw:
1. install.sh (POSIX sh) and install.ps1 at the root, ported from
   diluvium-drt's: start/install/get/help modes stamped per release,
   versions kept side by side, SHA256SUMS verification, mirror first then
   GitHub. Differences: no release log (aloelite's mirror releases.json
   lists dev builds), no completions (the CLI has no such verb), the FUSE
   daemon as an optional extra, and 0.4.0's old tarball names as a
   fallback, since 0.4.0 is still latest.
2. script/test-installer.sh and .ps1, offline against a file:// mirror;
   an `installer` job in main.yml.
3. release.yml stamps sixteen copies into the release assets.
4. README install section, RELEASING asset table, CHANGELOG.yaml entry.

Done: all four, on the one branch. install.sh passes its offline tests
under dash and bash, and was run live: `latest` installed v0.4.0 from its
tarball via the mirror, `dev` installed v0.5.0-dev.4's bare binaries,
and with the mirror unreachable both resolution and download fell back to
GitHub. install.ps1 and its tests could not be run here (no PowerShell in
the container); the `installer-windows` job in main.yml is their first
run. The README one-liners point at main's raw copy, which works once
merged; the stamped `latest/start.sh` URL works from the first release
that ships the installers (0.5.0). `ruff format --check` flags four
files, the same four on main before this change.

Learned: aloelite's mirror index lists dev builds, so it needs no
release-log branch; drt's does not, which is why drt has one.

Next: the owner merges the PR; then a dev tag would exercise the
`installers` job before 0.5.0 does.

## 2026-11-01 issue #8: what DRT's page filesystem needs

Plan: the owner asked directly for issue #8 end to end, outside the
roadmap (I0 Seed is still active and has no build). One PR on
claude/project-thread-03e809: (1) `Db::open_image` / `Db::snapshot` in
aloelite-core, used by `Fs.openMemory(bytes?)` / `fs.export()`, the Extism
plug-in and `Image`; normalise a WAL-mode header so native files load;
stop `check_sqlite_capabilities` masking real errors as "too old". (2)
`mount` sets `active` before commit in both engines (ACC-4; the
`set_mount_state` template already names "mount activate" as a consumer).
(3) README: pool.list() shows `-journal` files. (4) wasm-bindgen 0.2.127
via ego-platform main, only if the browser suite stays green.

Done: see the PR. Browser suite run locally in headless Chromium through
wasm-bindgen-test-runner's NO_HEADLESS server driven by Playwright
(chromedriver here cannot bind: no IPv6).

Learned: `ruff format --check .` fails on main today on Markdown code
blocks in README.md, doc/csi_driver.md, doc/volume_manager.md and
rust/aloelite-extism/README.md (a newer ruff formats Markdown); not from
this work.

## 2026-11-02 dev builds on merge, nightly.yml retired

Plan: find why no dev release followed dev.4; cut one now; replace the
cause rather than patch around it.

Done: dev.5 was tagged 2026-10-03 by nightly.yml before its build, the
build died in `image` (Docker Hub reset), and every nightly after skipped
because HEAD still matched the dev.5 tag. Dispatched the nightly by hand on
2026-11-02 for v0.5.0-dev.6 (0a23703, published). Then turned on
technoproj v0.4.1's `dev_builds`: release.yml runs on push to main, the
shared preflight allocates the number, and the publish leg creates the
tag after the build passes. release.yml rehearses on pull_request
instead of on a branch push. nightly.yml kept at the owner's word ("merge
to main produces a dev release; nightly is released nightly") but now
dispatches release.yml with the next number and pushes no tag, skipping
when HEAD already has one or a main release run is going. technoproj
needs no change.

Learned: the session clone's origin/main was stale (b4ea3be); fetch main
before branching.
