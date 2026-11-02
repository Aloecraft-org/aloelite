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
