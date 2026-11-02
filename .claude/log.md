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
