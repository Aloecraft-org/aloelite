# Changelog

All notable changes to Aloelite are recorded here.

Generated from `CHANGELOG.yaml`, which is the source of truth --
edit that file, then run `script/changelog.py generate`.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
`schema era` is the volume's on-disk `api_version`: a file written by
one era is readable by a build of that era, and an era bump is a
migration rather than a compatible change.

## Planned

### 0.4.0 -- schema era 2, the break-once migration

Not on `main`. Lives on `claude/aloelite-rust-port-assessment-732bo0`.

**One era bump carrying every structural change 0.4 needs**, taken once
at a release boundary rather than spread over several. Era 2 is a
migration, not a compatible change: an era-1 file is migrated forward on
open, and an era-2 file is refused by an era-1 build.

- `node` gains `uid`/`gid`/`mode` (ownership: POSIX and multitenant
  alike) and `atime`/`ctime`. `nlink` stays DERIVED from active-edge
  count -- a maintained counter can drift, a count cannot.
- **Timestamps become nanoseconds end to end.** The migration multiplies
  stored ms values by 1e6, guarded by a magnitude bound so a crash-rerun
  cannot double-apply. The uuid7 watermark stays ms (uuid7 is ms by
  spec).
- PI-1 narrows to containers: a single active parent keeps the container
  graph a tree while entries may hold many placements -- hardlinks.
- D-5: `edge.name` becomes a nullable per-placement name override;
  resolution, listings, rename and move operate on
  `coalesce(edge.name, node.name)`.
- NODE-2 widens to symlink/fifo/socket (devices refused, D-3); `mount`
  gains `access` (`ro`/`rw`) and `principal` (D-4); an `xattr` table is
  added.
- Ids become host-minted, and the volume watermark becomes an attach
  fence (D-1/D-2).

Also on that branch, and independent of the era bump: the manager split
into api/ui/engine, an `api-spec.yaml` HTTP contract projected by tests,
benchmarks (`script/benchmark.py`, `doc/BENCHMARKS.md`), and the
conformance suite extended with mount-level semantics and id-minting
vectors.

## [0.3.6] - 2026-08-31

`v0.3.6` &middot; schema era `1`

**WebDAV, promoted from an assessment to a working frontend.** The
protocol and engine work is verified here; the desktop-client behaviour
it targets is not, because CI has no Windows or macOS runner.

The frontend is off by default. It is a second, write-capable surface on
every volume, and the one feature that tempts a deployment past
loopback, so it does not appear by accident.

### Added

- **WebDAV frontend (`--webdav`), RFC 4918 compliance class 2.**
  LOCK/UNLOCK, Depth 0 and infinity, exclusive write locks, the `If:`
  header, and `lockdiscovery`/`supportedlock` reporting real state.
  Class 2 is what makes Finder mount read-write instead of read-only,
  and what stops the Windows redirector failing at first save.
- **TLS: `--tls-cert`/`--tls-key`, or `--tls-self-signed`** generated
  once and reused. Serving WebDAV off loopback without TLS is REFUSED,
  not warned about: the Basic password is the volume PIN.
- **Conditional requests on a strong ETag** built from
  `content.version`. A weak validator can never satisfy `If-Match` or
  `If-Range` (RFC 9110 8.8.3.2 requires strong comparison), so a weak
  etag would have reduced both headers to permanent no-ops.
- Engine: `lock`/`unlock`/`renew_lock` make a lock a first-class object that outlives any descriptor.
- Engine: `Descriptor.abort()`, plus abort-on-scope-exit, so a failed write discards its staging rather than committing a partial file.

### Changed

- Engine locks now guard placement, existence and metadata, not only content (ACC-11).
- `doc/WEBDAV.md` rewritten for class 2, including the correction that engine locks give no DAV-vs-DAV exclusion, because all DAV clients on a volume share one engine mount.

### Fixed

- **`If-Range` was splicing bytes across a change.** A resumed download
  of a file that had changed mid-transfer spliced the new bytes onto the
  prefix the client already held, producing a corrupt result that no
  error reported. A live corruption path, found by writing the
  conditional-request tests.
- `aloelite-web` died with `AttributeError` on Windows before binding a socket (`os.geteuid`) -- on the one platform where WebDAV is the only way in.

### Known issues

- No Windows or macOS runner in CI, so real Finder and Windows-redirector behaviour is untested here. That is what the rc was for.


## [0.3.5] - 2026-08-08

`v0.3.5` &middot; schema era `1`

**The manager learns to show you what is inside a file.** Rendering
rather than downloading, for the formats where a round trip to the
desktop was the only thing standing between a user and the content.

### Added

- Markdown rendering and syntax highlighting in the file viewer.
- Media preview for audio and video.
- A sketch pad: pointer/stylus input with pressure, colours, pen width, grid and dot backgrounds, undo, and SVG output.


## [0.3.4] - 2026-08-06

`v0.3.4` &middot; schema era `1`

**Path resolution stops paying per segment.** The change nobody notices
locally and everybody notices over a network, done before the language
ports so that nobody reimplements the slow version.

### Changed

- **One query per path, not one per segment.** `resolve()` and
  `resolve_parent()` folded `resolve_segment` over path components --
  one round trip each. Locally that is nearly free, which is exactly why
  it survived; over a network connection to a remote backend it is the
  difference between usable and unusable, and it is woven through every
  path-addressed operation. `resolution.resolve_path` is now a recursive
  CTE that walks the whole path in one statement.
- Web: explorer polished for phones, scale and consistency.
- CI: both workflows can be dispatched manually, so a tag whose run died for reasons unrelated to the code can be retried without deleting and re-pushing the tag.

### Fixed

- `..` containment is pinned by tests: a path may not escape its mount.


## [0.3.3] - 2026-08-06

`v0.3.3` &middot; schema era `1`

A polish and documentation release, cut the same day as 0.3.2 to get the
web-UI fixes out behind the incident hardening that preceded them.

### Added

- `script/browser_check.py`: the real-browser UI check, landed as a script rather than left as a manual procedure.
- `doc/HANDOFF-0.4.md`, recording what 0.4 needs and which decisions are still open.

### Changed

- Web: styled prompts, phone-size modals, and folders sorted first.


## [0.3.2] - 2026-08-06

`v0.3.2` &middot; schema era `1`

**Incident hardening.** On 2026-08-03, aloelite on stock Debian 12
(sqlite 3.40) failed mount with a `NOT NULL` IntegrityError, then failed
file creation with `no such function: jsonb`, surfacing through FUSE as
a bare `EIO`. Everything under Added and Fixed here exists because of
that, and the schema era stamp is what stops the same class of failure
recurring.

This release also carries the manager's authentication rework and the
four candidates tagged `v0.3.1rc1`..`v0.3.1rc4`.

### Added

- **A capability probe at open, the universal guard.** `Db.open` probes
  `jsonb()` AND `unixepoch('subsec')` -- not the version string -- and
  refuses with an actionable error naming the floor (>= 3.45).
  `'subsec'` needs its own probe because an unknown modifier RETURNS
  NULL rather than raising, which `printf` coerces to zero timestamps
  (so `new_uuid7` would silently mint ids in the 00000000 era) and
  `coalesce` turns into `NOT NULL` aborts.
- **Schema era stamped into `PRAGMA user_version`, with a
  derived-object refresh.** On open, an older or unstamped file has
  every trigger and view dropped and re-created from the CURRENT
  `schema.sql` -- they hold no data, so it is always safe -- and a
  newer-era file is refused with a clear "requires newer aloelite"
  error. Files no longer keep their creation-time derived-object text
  forever, which is the property that shipped `subsec` triggers to hosts
  that could not run them.
- **One sqlite import site (`aloelite/_sqlite.py`)**, preferring
  `pysqlite3` (the new `bundled-sqlite` extra: a statically linked
  modern sqlite, manylinux wheels only) and falling back to stdlib.
  Correctness rather than style: pysqlite3's exception classes are
  distinct from stdlib's, so an `except sqlite3.Error` against the wrong
  module never matches.
- Manager: cookie-auth mode with one engine mount per client; sessions are explicit bearer tokens.
- Web: a text editor and paste bin; UI assets vendored so the manager is self-contained.
- CLI: recursive `put`/`get` behind a single `-r` flag, following `cp -r` for destination semantics.
- CI: installs the `[fuse]` extra and runs the mounted-filesystem tests.
- Conformance: the spec is bound, the oracle shared, and the bytes pinned.

### Changed

- Host-supplied timestamps, rather than trusting sqlite to produce them.
- Manager: auth defaults off for 0.3.2; cookie mode stays opt-in until proven.

### Fixed

- Read descriptors track the committed pointer, so a read is coherent with what was committed rather than with what was staged. The bug was pinned as an xfail test first.
- FUSE: dirty state is keyed by inode, not by file handle, and `forget()` is implemented.
- Web: the 401 attach loop, and the stacked-modal PIN flow.
- Manager: detaching a client no longer strips the surviving clients' cipher.

### Security

- **ENC-3: a cipher/volume mismatch is refused instead of failing open.**
  The cipher lives on the CONNECTION and volumes live in the FILE, and
  nothing checked that the two agreed. They could disagree two ways --
  `attach()` binding a mount without installing any cipher, and mounting
  a second volume replacing the cipher connection-wide. Either way an
  encrypted volume was reachable with an identity cipher: reads returned
  stored ciphertext as though it were content, and writes put PLAINTEXT
  INTO AN ENCRYPTED VOLUME. Both silent, no error. This is the
  fail-closed fix; one cipher slot per connection is still the root
  cause, and per-mount ciphers are the real answer.


## [0.3.1] - 2026-07-25

`v0.3.1`

A CLI usability release.

### Changed

- CLI: PIN handling and convenience calls, with the docs updated to match.


## [0.3.0] - 2026-07-25

`v0.3.0`

**The web UI stops needing FUSE.** `aloelite-web` becomes an on-ramp
that runs anywhere Python does -- no Docker, no FUSE, no sudo -- which
is what makes a volume openable on a machine where a kernel mount is not
available or not permitted.

### Added

- `aloelite-web`: a FUSE-independent web UI, direct-only by default, with argparse, a `~/.aloelite` root and a `/` redirect.
- Web: upload progress, preview, rename/move/copy, and clean Ctrl-C port release.
- `--version` flag.
- `aloelite fuse ...` / `aloelite web ...` dispatch to the sub-tools before the main parser runs, with lazy imports so a missing FUSE dependency gives a clear message instead of a traceback.
- FUSE: permission bits persisted via the NODE-6 metadata map, which is what makes a git hook script executable and keeps it so across remounts.

### Changed

- Web UI binds localhost rather than 0.0.0.0 by default, and confirms the PIN.

### Fixed

- **FUSE committed streaming writes at RELEASE, which loses data.**
  FLUSH is delivered synchronously inside the app's `close()`; RELEASE
  arrives asynchronously afterward. Committing only at release left a
  window where an app had closed a file and renamed it into place while
  the bytes were still uncommitted staging, so a daemon death silently
  reverted the file. Found in the wild: git's ref update (write
  `main.lock`, close, rename over `main`) lost a commit's ref while the
  objects survived. Now committed at FLUSH, then converted to an rw
  handle, because a dup'd fd may legally write after FLUSH.
- Packaging: `config/` and `sql/` moved into the package and declared as package-data, so `sql-templates.yaml` and `schema.sql` ship in the wheel. Installed entry points previously resolved specs relative to site-packages and failed with `FileNotFoundError`.
