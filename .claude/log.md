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

## 2026-11-28 postgres benchmark suite

Plan: the owner asked directly for benchmarks, Postgres first, outside
the roadmap (I0 Seed still active). One PR on claude/project-thread-j1p9cd:
a `postgres` suite in a new `apps` group (bench/suites/apps.py) that runs
a real cluster with PGDATA on each mount and ext4, reports tps, latency,
space amplification before and after prune, and pg_amcheck after a
daemon SIGKILL; `fuse_session(allow_other=)`; doc/BENCHMARKS.md section;
PostgreSQL install step in benchmark.yml.

Done: smoke run here as root, ext4 and rust-fuse in both modes, all
pg_amcheck clean. Python daemon measured only with a local, uncommitted
fix (below).

Learned: (1) default retention (NULL, keep all) makes a PGDATA volume
grow ~27x in a minute, and prune + VACUUM returns none of it; keep-last-1
brings it to 0.74x. (2) the Python daemon ignores ctx.uid on mkdir/create
and mounts with default_permissions, so a root daemon serving another
uid via allow_other breaks initdb; the Rust daemon also records its own
uid but has no default_permissions. (3) a SIGKILLed daemon's mount row
blocks a remount for MOUNT_TTL_MS (5 min); the suite retires it and
prunes. (4) aloelite-fuse exits 1 with EINVAL after an external
`fusermount3 -u`. (5) non-root FUSE mounts fail in the cloud container;
CI runs non-root and is untested here.

## 2026-11-28 one version per fsync (issue #15 follow-up, item 1)

Plan: the owner picked item 1 of three proposed in the thread: batch a
file's dirty extents into one transaction and version. Add `write_ranges`
to mount-api.yaml, both engines, the api table (Value::items for the
browser and plug-in surfaces) and both conformance runners, with three
scenarios; switch both daemons' flush to it; re-run the postgres suite.
Stacked on PR #14's branch; it goes out as its own PR once #14 merges.

Done: all of the above. Overlay unit tests in both daemons assert 200
scattered writes + one flush = one version. A refused flush now keeps
the extents buffered (it used to commit some and lose the rest in Rust).

Measured (smoke, rust-fuse, before -> after): `pgbench -i` 34 s -> 4.2 s
plain, 71 s -> 3.9 s convergent; space 27.6x -> 10.2x plain, 24.8x ->
7.4x convergent. TPC-B unchanged (~1 fsync per transaction already
meant one version per transaction); that cost is item 3 (in-place or
delta manifests, smaller chunks). The Python daemon still cannot be
measured as root (#15).

Added on the owner's ask, same PR (#16): `--chunk-size SIZE` on both
daemons and both CLIs (a global there, like the pin flags; cli.yaml
updated). Refused where no volume is created and for an existing volume
of another size (CV-1). Measured on postgres, rust-fuse plain: 1M 121
tps / 10.2x, 64K 194 tps / 2.6x, 16K 64 tps / 2.2x; the 16K loss is
the per-version full manifest copy (CV-4). Also in #16: a token for
setup-geckodriver, whose anonymous release lookup was rate-limited.
