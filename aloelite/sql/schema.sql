-- ============================================================================
-- SQLite Filesystem Schema
--
-- Maps to the requirements document. Where an invariant is enforced
-- declaratively, the constraint is noted with its requirement id. Invariants
-- that cannot be expressed in the schema (EDGE-4 container-type check, PI-5
-- reparent ancestor check, ACC-5 mount-point interpretation, VOL-2 no-edge-to-
-- root) are deliberately absent here and belong to the Mount API.
--
-- ID GENERATION (era 2, doc/DECISIONS.md D-1/D-2). Ids are HOST-MINTED; every
-- INSERT arrives with an explicit id. The era-1 insert-view/trigger minting
-- idiom is retired (the era refresh removes those derived objects from old
-- files). Triggers still DEFEND invariants; they no longer generate.
--
--   * node / edge ids are uuid7s minted by a per-volume MonotonicMint in the
--     host (strictly increasing per mount session; 12-bit counter in rand_a,
--     1ms borrow on overflow). volume.wm_ts/wm_seq is the volume's HIGH-WATER
--     MARK: hosts fence their mint at attach (max of clock and mark) and
--     advance the mark inside each write transaction, so no session can mint
--     at or below anything the volume has recorded — clock regression and
--     failover skew included. Ordering contract: strict within a mount,
--     strict across mounts beyond the coordination window, arbitrary within
--     it (concurrent unlocked writers have no defined "later").
--   * volume / mount / lock ids are stateless uuid7 (no ordering promise).
--
-- TIMESTAMPS are INTEGER nanoseconds since the unix epoch (era 2; era 1
-- stored milliseconds — the era-2 migration multiplies stored values by 1e6).
-- ============================================================================

PRAGMA foreign_keys = ON;

-- ----------------------------------------------------------------------------
-- Base tables
-- ----------------------------------------------------------------------------

-- Identity. NODE-1..5. volume_id is nullable (recovery/import + bootstrapping
-- the circular node<->volume reference); a null volume_id is an error state in
-- a healthy filesystem, surfaced by health_anomaly below.
CREATE TABLE IF NOT EXISTS node (
  node_id    TEXT    PRIMARY KEY,
  -- NODE-2 vocabulary is enforced by node_guard_type below, NOT by a CHECK:
  -- a CHECK fossilizes into every file's table DDL (tables are never
  -- rebuilt), so widening the type set later -- symlink, fifo, socket,
  -- device for the POSIX era -- would need a 12-step table rebuild per
  -- file. A guard trigger is a derived object: the era-refresh rewrite on
  -- open updates it in place, so new types are a trigger change, never a
  -- table migration.
  type       TEXT    NOT NULL,                                          -- NODE-2
  name       TEXT    NOT NULL,                                          -- NODE-3 (a placement may override it: edge.name)
  created_at INTEGER NOT NULL,                                          -- NODE-4 (ns)
  modified_at INTEGER,                                                  -- own content/metadata change, NOT placement; null => never tracked (read as created_at)
  volume_id  TEXT    REFERENCES volume (volume_id),
  metadata   BLOB,                                                      -- NODE-6: shallow {string:string} as JSONB; NULL == empty map
  -- era 2: ownership + POSIX metadata (all nullable; hosts fall back to
  -- process defaults when NULL, so imported/era-1 nodes need no backfill).
  -- These same columns serve POSIX ownership and multitenant ownership.
  uid        INTEGER,
  gid        INTEGER,
  mode       INTEGER,                                                   -- permission bits (07777); type lives in `type`, never here
  atime      INTEGER,                                                   -- ns; set via setattr only (noatime semantics), NULL => read as modified
  ctime      INTEGER                                                    -- ns; inode-change time, bumped by the touch triggers below
) STRICT;
-- nlink is deliberately DERIVED (count of active edges for entries; 2+subdirs
-- for containers), never a stored column: a maintained counter can drift, a
-- count over edge_to_active cannot.

-- Payload, split from metadata so traversal never touches blobs. IO-1, IO-6.
-- content_hash is reserved for the future Merkle leaf (EXT-2); unused for now.
-- content is now the per-Entry MANIFEST ROW: it carries the committed version
-- pointer (CV-3, the sole definition of current bytes), a materialized total
-- size (so get_node needs no chunk join), the reserved whole-payload hash
-- (EXT-2), and a single keep-last-N retention policy (CV-6). The inline payload
-- is gone. bytes live in the chunk pool, referenced through content_version.
CREATE TABLE IF NOT EXISTS content (
  node_id        TEXT PRIMARY KEY REFERENCES node (node_id) ON DELETE CASCADE,
  version        INTEGER NOT NULL DEFAULT 0,   -- CV-3: committed version pointer
  size           INTEGER NOT NULL DEFAULT 0,   -- materialized total bytes
  content_hash   BLOB,                         -- reserved whole-payload hash (EXT-2); unused
  retention_keep INTEGER                       -- CV-6: keep-last-N; NULL = keep all superseded versions
) STRICT;

-- CV-1/CV-2: content-addressed immutable chunk pool. chunk_hash folds the byte
-- length into the address (hash(len || bytes)) so a short final/small chunk can
-- never collide with a full chunk that shares leading bytes. Chunks are shared
-- across entries and versions and never mutated in place.
-- ENC-2: N_c is a convergent salt (derived from plaintext to preserve dedup);
-- enc_tag is the ChaCha20-Poly1305 authentication tag. Data stored as: enc_tag || ciphertext,
-- or as separate columns for clarity. Using separate columns here.
CREATE TABLE IF NOT EXISTS content_chunk (
  chunk_hash TEXT    PRIMARY KEY,              -- content address incl. length
  data       BLOB    NOT NULL,                 -- encrypted ciphertext
  length     INTEGER NOT NULL,                 -- plaintext length (used for offset math)
  N_c        BLOB    NOT NULL,                 -- ENC-2: convergent salt (16 bytes)
  enc_tag    BLOB    NOT NULL                  -- ENC-2: ChaCha20-Poly1305 tag (16 bytes)
) STRICT;

-- CV-4: the ordered manifest. one row per chunk reference. Composite PK makes
-- position-within-version unique and pins reassembly order. chunk_hash FK into
-- the pool; the separate index below supports the GC reverse walk
-- (chunk -> versions). `proof` reserves a per-reference Merkle membership slot
-- (EXT-2); it is never populated here.
CREATE TABLE IF NOT EXISTS content_version (
  content_id  TEXT    NOT NULL REFERENCES node (node_id) ON DELETE CASCADE,
  version     INTEGER NOT NULL,
  chunk_index INTEGER NOT NULL,
  chunk_hash  TEXT    NOT NULL REFERENCES content_chunk (chunk_hash),
  proof       BLOB,
  PRIMARY KEY (content_id, version, chunk_index)
) STRICT;

-- GC reverse walk: given a chunk, which version references it (for sweeping).
CREATE INDEX IF NOT EXISTS content_version_chunk ON content_version (chunk_hash);

-- Origin. VOL-1..4. root_node_id nullable for bootstrapping; UNIQUE so a node
-- roots at most one volume. wm_ts/wm_seq are this volume's id HIGH-WATER MARK
-- (D-2): the highest (ms, seq) any session has recorded. Hosts fence their
-- mint here at attach and advance it (monotonically) per write transaction.
-- ENC-3: enc_mode reserves the encryption strategy (none/convergent/random for chunks).
-- Defaults to 'convergent' (dedup + equality leakage). 'random' sacrifices dedup
-- for zero equality leakage; 'none' for unencrypted (debugging only).
-- wrapped_key is K_v (the volume content key) sealed under K_u = Argon2id(PIN, H_v),
-- where H_v = SHA256(volume_id || root_node_id) is derived (never stored); wrap_nonce
-- is the AEAD nonce for that seal. Both NULL on an unencrypted ('none') volume.
CREATE TABLE IF NOT EXISTS volume (
  volume_id    TEXT    PRIMARY KEY,
  root_node_id TEXT    UNIQUE REFERENCES node (node_id),                -- VOL-2
  name         TEXT,
  created_at   INTEGER NOT NULL,
  api_version  INTEGER NOT NULL DEFAULT 1,                              -- migration hub: a node/edge finds its schema era via its volume_id
  chunk_size   INTEGER NOT NULL DEFAULT 1048576,                        -- CV-1: per-volume chunk size, fixed at creation, immutable
  wm_ts        INTEGER NOT NULL DEFAULT 0,
  wm_seq       INTEGER NOT NULL DEFAULT 0,
  enc_mode     TEXT    NOT NULL DEFAULT 'none'
                       CHECK (enc_mode IN ('none', 'convergent', 'random')),  -- ENC-3
  wrapped_key  BLOB,                                                    -- ENC-2: K_v sealed under K_u (NULL if enc_mode='none')
  wrap_nonce   BLOB                                                     -- ENC-2: AEAD nonce for wrapped_key
) STRICT;

-- Placement. EDGE-1..6. volume_id is kept on the edge as an authoritative copy
-- (deliberate redundancy, verified by health_anomaly). archived edges are
-- retained for recovery (EDGE-5, PI-7).
CREATE TABLE IF NOT EXISTS edge (
  edge_id   TEXT    PRIMARY KEY,
  from_id   TEXT    NOT NULL REFERENCES node (node_id),                 -- EDGE-4 (container check is procedural)
  to_id     TEXT    NOT NULL REFERENCES node (node_id),
  volume_id TEXT    NOT NULL REFERENCES volume (volume_id),
  archived  INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
  -- era 2 (D-5): per-placement name override for multi-placement (hardlinked)
  -- entries. NULL => the node's own name. Resolution and listings match on
  -- coalesce(edge.name, node.name). Rename is a placement operation in POSIX
  -- (it edits a directory entry, not the inode): it sets THIS, and refreshes
  -- node.name too only when this is the node's sole active placement.
  name      TEXT
) STRICT;

-- Guard triggers: refuse-only enforcement for invariants the schema cannot
-- express as constraints. These never DO work, they only REJECT. so they
-- cannot drift in behavior and they protect the file equally no matter which
-- of the four implementations is writing. Active (work-performing) logic stays
-- in the Mount API. Fire on every base-table insert, including those issued by
-- the edge_new insert-view.
-- NODE-2: the node-type vocabulary lives here (not in a table CHECK) so the
-- era refresh can widen it without a table rebuild. Refuse-only, like every
-- guard: it rejects, it never does work.
-- era 2 (D-3): the vocabulary widens to symlink/fifo/socket. Device nodes are
-- REFUSED by decision, not omission — a data filesystem storing device nodes
-- is a security surface with no identified use case; a future era can widen
-- this same trigger if that changes. symlink/fifo/socket place like entries
-- (they are leaves; EDGE-4 still requires from_id to be a container).
CREATE TRIGGER IF NOT EXISTS node_guard_type BEFORE INSERT ON node
WHEN NEW.type NOT IN ('container', 'entry', 'symlink', 'fifo', 'socket')
BEGIN
  SELECT RAISE(ABORT, 'NODE-2: unknown node type');
END;

CREATE TRIGGER IF NOT EXISTS node_guard_type_upd BEFORE UPDATE OF type ON node
WHEN NEW.type NOT IN ('container', 'entry', 'symlink', 'fifo', 'socket')
BEGIN
  SELECT RAISE(ABORT, 'NODE-2: unknown node type');
END;

CREATE TRIGGER IF NOT EXISTS edge_guard_from_type BEFORE INSERT ON edge
WHEN (SELECT type FROM node WHERE node_id = NEW.from_id) <> 'container'   -- EDGE-4
BEGIN
  SELECT RAISE(ABORT, 'EDGE-4: edge.from_id must reference a container');
END;

-- era 2: PI-1 narrows to CONTAINERS (single active parent => the container
-- graph stays a tree and PI-4's cycle argument holds). Entries may hold
-- multiple active placements — hardlinks (EXT-1's reserved relaxation).
-- Refuse-only triggers replace the era-1 partial unique index, which could
-- not consult node.type; the era-2 migration drops that index.
CREATE TRIGGER IF NOT EXISTS edge_guard_single_parent BEFORE INSERT ON edge
WHEN NEW.archived = 0
  AND (SELECT type FROM node WHERE node_id = NEW.to_id) = 'container'
  AND EXISTS (SELECT 1 FROM edge WHERE to_id = NEW.to_id AND archived = 0)
BEGIN
  SELECT RAISE(ABORT, 'PI-1: container already has an active placement');
END;

CREATE TRIGGER IF NOT EXISTS edge_guard_single_parent_upd
BEFORE UPDATE OF archived ON edge
WHEN NEW.archived = 0 AND OLD.archived = 1
  AND (SELECT type FROM node WHERE node_id = NEW.to_id) = 'container'
  AND EXISTS (SELECT 1 FROM edge
              WHERE to_id = NEW.to_id AND archived = 0
                AND edge_id <> NEW.edge_id)
BEGIN
  SELECT RAISE(ABORT, 'PI-1: container already has an active placement');
END;

CREATE TRIGGER IF NOT EXISTS edge_guard_volume BEFORE INSERT ON edge        -- PI-6
WHEN ( (SELECT volume_id FROM node WHERE node_id = NEW.from_id) IS NOT NULL
       AND (SELECT volume_id FROM node WHERE node_id = NEW.from_id) <> NEW.volume_id )
  OR ( (SELECT volume_id FROM node WHERE node_id = NEW.to_id) IS NOT NULL
       AND (SELECT volume_id FROM node WHERE node_id = NEW.to_id) <> NEW.volume_id )
BEGIN
  SELECT RAISE(ABORT, 'PI-6: edge.volume_id must match its endpoints'' volume');
END;

-- modified_at touch triggers. Bump a node's modified_at on a change to its own
-- content or own metadata (name). NOT on placement (a move changes edges, not
-- the node row, so modified_at deliberately stays put; modified vs moved are
-- different questions). Schema-side so the bump is identical across all four
-- implementations rather than per-Mount-API discipline. A content write now
-- advances the committed version pointer (UPDATE OF version), so that is what
-- bumps modified_at. create_content/copy/pack establish content via INSERT
-- (which does not fire this trigger), keeping a fresh file's
-- modified_at == created_at.
CREATE TRIGGER IF NOT EXISTS node_touch_content
AFTER UPDATE OF version, content_hash ON content
BEGIN
  UPDATE node SET modified_at = cast(unixepoch('subsec') * 1000000000 AS INTEGER),
                  ctime       = cast(unixepoch('subsec') * 1000000000 AS INTEGER)
  WHERE node_id = NEW.node_id;
END;

CREATE TRIGGER IF NOT EXISTS node_touch_name
AFTER UPDATE OF name ON node
WHEN NEW.name <> OLD.name                       -- skip no-op renames
BEGIN
  UPDATE node SET modified_at = cast(unixepoch('subsec') * 1000000000 AS INTEGER),
                  ctime       = cast(unixepoch('subsec') * 1000000000 AS INTEGER)
  WHERE node_id = NEW.node_id;                  -- UPDATE OF modified_at, not name: no recursion
END;

-- era 2: ctime (inode-change time) bumps. Ownership/permission changes touch
-- ctime but NOT modified_at (POSIX: chmod is a status change, not a content
-- change); link-count changes (a placement appearing, archiving, or
-- unarchiving) do the same. UPDATE OF ctime never refires these.
CREATE TRIGGER IF NOT EXISTS node_touch_owner
AFTER UPDATE OF uid, gid, mode ON node
BEGIN
  UPDATE node SET ctime = cast(unixepoch('subsec') * 1000000000 AS INTEGER)
  WHERE node_id = NEW.node_id;
END;

CREATE TRIGGER IF NOT EXISTS edge_touch_ctime
AFTER INSERT ON edge
WHEN NEW.archived = 0
BEGIN
  UPDATE node SET ctime = cast(unixepoch('subsec') * 1000000000 AS INTEGER)
  WHERE node_id = NEW.to_id;
END;

CREATE TRIGGER IF NOT EXISTS edge_touch_ctime_arch
AFTER UPDATE OF archived ON edge
WHEN NEW.archived <> OLD.archived
BEGIN
  UPDATE node SET ctime = cast(unixepoch('subsec') * 1000000000 AS INTEGER)
  WHERE node_id = NEW.to_id;
END;

-- NODE-6: there is intentionally NO node_touch_metadata trigger. Metadata is
-- node-level annotation, not content; setting it must NOT bump modified_at
-- (mirrors the placement rule: a move doesn't bump it either). Do not add one.

-- Access session. ACC-1..5. Bound to one volume, anchored at a mount point.
-- ENC-1: N_m is the ephemeral mount nonce, random per mount, used to derive the
-- session mount key from the user-held token. Lives only for the mount duration.
CREATE TABLE IF NOT EXISTS mount (
  mount_id    TEXT    PRIMARY KEY,
  volume_id   TEXT    NOT NULL REFERENCES volume (volume_id),           -- ACC-1
  mount_point TEXT    NOT NULL REFERENCES node (node_id),               -- ACC-2
  state       TEXT    NOT NULL DEFAULT 'new'
                      CHECK (state IN ('new', 'active', 'unmounted')), -- ACC-4
  expires_at  INTEGER,                                                  -- ACC-3 (ttl as absolute instant, ns)
  created_at  INTEGER NOT NULL,                                         -- ns
  N_m         BLOB    NOT NULL,                                         -- ENC-1: ephemeral mount nonce (16 bytes)
  -- era 2 (D-4 / HANDOFF-0.4 §4.3): mount policy. access vocabulary lives in
  -- mount_guard_access (a trigger, like NODE-2) so a future era can widen it.
  access      TEXT    NOT NULL DEFAULT 'rw',                            -- 'ro' | 'rw'
  principal   TEXT                                                      -- tenant/user identity for policy + future ACLs; NULL = unattributed
) STRICT;

-- Locks. ACC-6..9. Scoped to one mount; cascade is a dangle-safety net, not
-- the reclamation path (reclamation is lazy prune, ACC-10).
CREATE TABLE IF NOT EXISTS lock (
  lock_id     TEXT    PRIMARY KEY,
  mount_id    TEXT    NOT NULL REFERENCES mount (mount_id) ON DELETE CASCADE,
  node_id     TEXT    NOT NULL REFERENCES node (node_id),
  read_count  INTEGER NOT NULL DEFAULT 0,                               -- ACC-8 (recorded, not yet enforced)
  write_count INTEGER NOT NULL DEFAULT 0,
  expires_at  INTEGER,                                                  -- ns
  created_at  INTEGER NOT NULL                                          -- ns
) STRICT;

-- era 2 (D-4): mount access vocabulary, guard-trigger style like NODE-2.
CREATE TRIGGER IF NOT EXISTS mount_guard_access BEFORE INSERT ON mount
WHEN NEW.access NOT IN ('ro', 'rw')
BEGIN
  SELECT RAISE(ABORT, 'ACC: unknown mount access mode');
END;

CREATE TRIGGER IF NOT EXISTS mount_guard_access_upd BEFORE UPDATE OF access ON mount
WHEN NEW.access NOT IN ('ro', 'rw')
BEGIN
  SELECT RAISE(ABORT, 'ACC: unknown mount access mode');
END;

-- era 2: extended attributes (user.* namespace through FUSE). A separate
-- table, not NODE-6 metadata: xattr values are binary and can be large, and
-- NODE-6 is a shallow {string:string} map by contract. Deleting a node
-- cascades its xattrs.
CREATE TABLE IF NOT EXISTS xattr (
  node_id TEXT NOT NULL REFERENCES node (node_id) ON DELETE CASCADE,
  name    TEXT NOT NULL,
  value   BLOB NOT NULL,
  PRIMARY KEY (node_id, name)
) STRICT;

-- ----------------------------------------------------------------------------
-- Indexes
-- ----------------------------------------------------------------------------

-- PI-1 (era 2): enforcement moved to the edge_guard_single_parent triggers
-- above, scoped to containers so entries can hold multiple placements
-- (hardlinks). The era-1 partial unique index edge_active_placement is
-- dropped by the era-2 migration; lookups are covered by edge_to_active.

CREATE INDEX IF NOT EXISTS edge_from_active ON edge (from_id) WHERE archived = 0; -- child enumeration
CREATE INDEX IF NOT EXISTS edge_to_active   ON edge (to_id)   WHERE archived = 0; -- active parent / path walk
CREATE INDEX IF NOT EXISTS edge_to_any      ON edge (to_id);                      -- volatility (any edge, PI-3)
CREATE INDEX IF NOT EXISTS edge_volume      ON edge (volume_id);                  -- volume-scoped sweeps
CREATE INDEX IF NOT EXISTS lock_mount       ON lock (mount_id);

-- ----------------------------------------------------------------------------
-- Operational views
-- ----------------------------------------------------------------------------

CREATE VIEW IF NOT EXISTS active_edge AS
  SELECT * FROM edge WHERE archived = 0;

-- Each node's single active parent (PI-1 guarantees at most one). Primitive
-- for path resolution.
CREATE VIEW IF NOT EXISTS node_parent AS
  SELECT to_id AS node_id, from_id AS parent_id, volume_id, edge_id
  FROM active_edge;

-- Transitive ancestors with depth (IO-5). The depth guard makes a cycle
-- terminate instead of looping; a node appearing as its own ancestor here is
-- the cycle tripwire (PI-5 backstop).
CREATE VIEW IF NOT EXISTS node_ancestor AS
  WITH RECURSIVE walk (node_id, ancestor_id, depth) AS (
    SELECT node_id, parent_id, 1 FROM node_parent
    UNION ALL
    SELECT w.node_id, np.parent_id, w.depth + 1
    FROM walk w JOIN node_parent np ON np.node_id = w.ancestor_id
    WHERE w.depth < 256
  )
  SELECT * FROM walk;

-- Descendant closure: every node paired with each node in its subtree, in
-- TOP-DOWN canonical order (depth, then edge_id, then node_id). This is the
-- shared read primitive that copy, pack, and remove_recursive enumerate from,
-- so the walk order is defined once here rather than re-imposed per caller.
-- Walks active edges only (via node_parent), so the detached graveyard is
-- never included. Callers filter by root_id; depth 0 is the root itself.
CREATE VIEW IF NOT EXISTS subtree AS
  WITH RECURSIVE walk (root_id, node_id, parent_id, edge_id, depth) AS (
    SELECT n.node_id, n.node_id, NULL, NULL, 0 FROM node n
    UNION ALL
    SELECT w.root_id, np.node_id, np.parent_id, np.edge_id, w.depth + 1
    FROM walk w JOIN node_parent np ON np.parent_id = w.node_id
    WHERE w.depth < 256
  )
  SELECT * FROM walk
  ORDER BY root_id, depth, edge_id, node_id;

-- Children of a container, with NODE-5 visibility resolved (greatest node_id
-- per EFFECTIVE name is visible). Ordered per EXT-3 (edge_id, then node_id).
-- era 2 (D-5): the effective name is coalesce(edge.name, node.name) — a
-- hardlinked entry may carry a different name per placement.
CREATE VIEW IF NOT EXISTS directory_listing AS
  SELECT
    ae.from_id AS container_id,
    n.node_id,
    coalesce(ae.name, n.name) AS name,
    n.type,
    ae.edge_id,
    (n.node_id = (
       SELECT max(n2.node_id)
       FROM active_edge ae2 JOIN node n2 ON n2.node_id = ae2.to_id
       WHERE ae2.from_id = ae.from_id
         AND coalesce(ae2.name, n2.name) = coalesce(ae.name, n.name)
    )) AS visible
  FROM active_edge ae JOIN node n ON n.node_id = ae.to_id
  ORDER BY ae.from_id, ae.edge_id, n.node_id;

-- PI-3: no incoming edge of any kind, and not a volume root. Node-side input
-- to prune.
CREATE VIEW IF NOT EXISTS volatile_node AS
  SELECT n.node_id
  FROM node n
  WHERE NOT EXISTS (SELECT 1 FROM edge   e WHERE e.to_id        = n.node_id)
    AND NOT EXISTS (SELECT 1 FROM volume v WHERE v.root_node_id = n.node_id);

-- The recoverable graveyard: detached (has an archived incoming edge) but no
-- active placement, and not a volume root. This is the state between remove()
-- and prune() — recoverable for as long as the archived edge survives (PI-7).
-- Distinct from volatile_node (which has NO edge at all and is purgeable).
CREATE VIEW IF NOT EXISTS detached_node AS
  SELECT n.node_id
  FROM node n
  WHERE EXISTS     (SELECT 1 FROM edge e WHERE e.to_id = n.node_id AND e.archived = 1)
    AND NOT EXISTS (SELECT 1 FROM active_edge ae WHERE ae.to_id = n.node_id)
    AND NOT EXISTS (SELECT 1 FROM volume v WHERE v.root_node_id = n.node_id);

-- CV-7: which (content_id, version) pairs survive a content prune. Resolved
-- BEFORE any chunk is collected. Rules:
--   * the committed version is ALWAYS retained;
--   * superseded versions BELOW committed are retained per the node's policy:
--       retention_keep IS NULL  -> keep all of them;
--       retention_keep = N      -> keep the (N-1) highest below committed
--                                  (committed itself is the Nth);
--   * versions ABOVE committed are NEVER retained — they are incomplete/aborted
--     writes (staged chunks whose pointer swap never happened), so this view is
--     also what makes crash-orphans collectable.
CREATE VIEW IF NOT EXISTS retained_version AS
  WITH below AS (
    SELECT
      cv.content_id,
      cv.version,
      c.retention_keep AS keep,
      row_number() OVER (
        PARTITION BY cv.content_id ORDER BY cv.version DESC
      ) AS rnk
    FROM (SELECT DISTINCT content_id, version FROM content_version) cv
    JOIN content c ON c.node_id = cv.content_id
    WHERE cv.version < c.version          -- superseded history, below committed
  )
  -- committed version is ALWAYS retained
  SELECT DISTINCT cv2.content_id, cv2.version
  FROM content_version cv2
  JOIN content c2 ON c2.node_id = cv2.content_id
  WHERE cv2.version = c2.version
  UNION
  -- superseded versions kept per the node's policy (NULL = keep all)
  SELECT content_id, version
  FROM below
  WHERE keep IS NULL OR rnk <= max(keep - 1, 0);

CREATE VIEW IF NOT EXISTS valid_mount AS
  SELECT * FROM mount
  WHERE state <> 'unmounted'
    AND (expires_at IS NULL OR expires_at > cast(unixepoch('subsec') * 1000000000 AS INTEGER));

-- ACC-9: valid only while its mount is valid and its own ttl holds.
CREATE VIEW IF NOT EXISTS valid_lock AS
  SELECT l.*
  FROM lock l JOIN valid_mount vm ON vm.mount_id = l.mount_id
  WHERE l.expires_at IS NULL OR l.expires_at > cast(unixepoch('subsec') * 1000000000 AS INTEGER);

-- Lock-side input to prune (ACC-10): everything not currently valid.
CREATE VIEW IF NOT EXISTS prunable_lock AS
  SELECT l.* FROM lock l
  WHERE l.lock_id NOT IN (SELECT lock_id FROM valid_lock);

-- Lock joined to its mount with computed validity, so the streaming layer can
-- answer "is this descriptor's lock still good" (ACC-9) in one read.
CREATE VIEW IF NOT EXISTS mount_lock AS
  SELECT
    l.lock_id, l.mount_id, l.node_id,
    l.expires_at AS lock_expires,
    m.state      AS mount_state,
    m.expires_at AS mount_expires,
    (m.state <> 'unmounted'
     AND (m.expires_at IS NULL OR m.expires_at > cast(unixepoch('subsec') * 1000000000 AS INTEGER))
     AND (l.expires_at IS NULL OR l.expires_at > cast(unixepoch('subsec') * 1000000000 AS INTEGER))
    ) AS valid
  FROM lock l JOIN mount m ON m.mount_id = l.mount_id;

-- ----------------------------------------------------------------------------
-- Health views — tripwires for the relaxations we took deliberately. All
-- should be empty in a consistent filesystem.
-- ----------------------------------------------------------------------------

CREATE VIEW IF NOT EXISTS health_anomaly AS
  -- edge.volume_id disagreeing with either endpoint's node.volume_id (the
  -- deliberate-redundancy tripwire)
  SELECT 'edge_volume_mismatch' AS kind, e.edge_id AS id
  FROM edge e
  JOIN node f ON f.node_id = e.from_id
  JOIN node t ON t.node_id = e.to_id
  WHERE (f.volume_id IS NOT NULL AND f.volume_id <> e.volume_id)
     OR (t.volume_id IS NOT NULL AND t.volume_id <> e.volume_id)
  UNION ALL
  -- non-volatile node with no volume (null FK left over from import/recovery)
  SELECT 'node_without_volume', n.node_id
  FROM node n
  WHERE n.volume_id IS NULL
    AND EXISTS (SELECT 1 FROM edge e WHERE e.to_id = n.node_id)
  UNION ALL
  -- volume with no root, or a root that no longer exists
  SELECT 'volume_without_root', v.volume_id
  FROM volume v
  WHERE v.root_node_id IS NULL
     OR NOT EXISTS (SELECT 1 FROM node n WHERE n.node_id = v.root_node_id)
  UNION ALL
  -- a node that is its own ancestor (cycle escaped the reparent guard)
  SELECT 'cycle', a.node_id
  FROM node_ancestor a
  WHERE a.node_id = a.ancestor_id;

