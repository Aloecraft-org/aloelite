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
