-- ============================================================================
-- SQLite Filesystem Schema
--
-- Maps to the requirements document. Where an invariant is enforced
-- declaratively, the constraint is noted with its requirement id. Invariants
-- that cannot be expressed in the schema (EDGE-4 container-type check, PI-5
-- reparent ancestor check, ACC-5 mount-point interpretation, VOL-2 no-edge-to-
-- root) are deliberately absent here and belong to the Mount API.
--
-- ID GENERATION. SQLite BEFORE triggers cannot modify NEW, and DEFAULT cannot
-- do a cross-table read-modify-write, so auto-id generation uses the
-- insert-view idiom: INSERT INTO <x>_new (...) fires an INSTEAD OF INSERT
-- trigger that mints the id and writes the base table. Direct INSERT INTO <x>
-- (...) with an explicit id is the import/recovery path and bypasses
-- generation entirely.
--
--   * node / edge ids are monotonic within their volume, drawn from the
--     volume's (wm_ts, wm_seq) watermark. 12-bit counter in uuid7 rand_a
--     (4096 ids/ms/volume); on overflow the timestamp borrows 1ms forward
--     (spin is not expressible in a trigger).
--   * volume / mount / lock ids are stateless uuid7 (no ordering requirement).
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
  type       TEXT    NOT NULL CHECK (type IN ('container', 'entry')),   -- NODE-2
  name       TEXT    NOT NULL,                                          -- NODE-3
  created_at INTEGER NOT NULL,                                          -- NODE-4
  modified_at INTEGER,                                                  -- own content/metadata change, NOT placement; null => never tracked (read as created_at)
  volume_id  TEXT    REFERENCES volume (volume_id),
  metadata   BLOB                                                       -- NODE-6: shallow {string:string} as JSONB; NULL == empty map
) STRICT;

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
-- roots at most one volume. wm_ts/wm_seq are this volume's id watermark.
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
  archived  INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1))
) STRICT;

-- Guard triggers: refuse-only enforcement for invariants the schema cannot
-- express as constraints. These never DO work, they only REJECT. so they
-- cannot drift in behavior and they protect the file equally no matter which
-- of the four implementations is writing. Active (work-performing) logic stays
-- in the Mount API. Fire on every base-table insert, including those issued by
-- the edge_new insert-view.
CREATE TRIGGER IF NOT EXISTS edge_guard_from_type BEFORE INSERT ON edge
WHEN (SELECT type FROM node WHERE node_id = NEW.from_id) <> 'container'   -- EDGE-4
BEGIN
  SELECT RAISE(ABORT, 'EDGE-4: edge.from_id must reference a container');
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
  UPDATE node SET modified_at = cast(unixepoch('subsec') * 1000 AS INTEGER)
  WHERE node_id = NEW.node_id;
END;

CREATE TRIGGER IF NOT EXISTS node_touch_name
AFTER UPDATE OF name ON node
WHEN NEW.name <> OLD.name                       -- skip no-op renames
BEGIN
  UPDATE node SET modified_at = cast(unixepoch('subsec') * 1000 AS INTEGER)
  WHERE node_id = NEW.node_id;                  -- UPDATE OF modified_at, not name: no recursion
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
  expires_at  INTEGER,                                                  -- ACC-3 (ttl as absolute instant)
  created_at  INTEGER NOT NULL,
  N_m         BLOB    NOT NULL                                          -- ENC-1: ephemeral mount nonce (16 bytes)
) STRICT;

-- Locks. ACC-6..9. Scoped to one mount; cascade is a dangle-safety net, not
-- the reclamation path (reclamation is lazy prune, ACC-10).
CREATE TABLE IF NOT EXISTS lock (
  lock_id     TEXT    PRIMARY KEY,
  mount_id    TEXT    NOT NULL REFERENCES mount (mount_id) ON DELETE CASCADE,
  node_id     TEXT    NOT NULL REFERENCES node (node_id),
  read_count  INTEGER NOT NULL DEFAULT 0,                               -- ACC-8 (recorded, not yet enforced)
  write_count INTEGER NOT NULL DEFAULT 0,
  expires_at  INTEGER,
  created_at  INTEGER NOT NULL
) STRICT;

-- ----------------------------------------------------------------------------
-- Indexes
-- ----------------------------------------------------------------------------

-- PI-1: at most one ACTIVE incoming edge per node per volume. Partial unique,
-- not plain UNIQUE, so an archived old placement and a new active one coexist
-- during a move.
CREATE UNIQUE INDEX IF NOT EXISTS edge_active_placement
  ON edge (volume_id, to_id) WHERE archived = 0;

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
-- per name is visible). Ordered per EXT-3 (edge_id, then node_id).
CREATE VIEW IF NOT EXISTS directory_listing AS
  SELECT
    ae.from_id AS container_id,
    n.node_id,
    n.name,
    n.type,
    ae.edge_id,
    (n.node_id = (
       SELECT max(n2.node_id)
       FROM active_edge ae2 JOIN node n2 ON n2.node_id = ae2.to_id
       WHERE ae2.from_id = ae.from_id AND n2.name = n.name
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
    AND (expires_at IS NULL OR expires_at > cast(unixepoch('subsec') * 1000 AS INTEGER));

-- ACC-9: valid only while its mount is valid and its own ttl holds.
CREATE VIEW IF NOT EXISTS valid_lock AS
  SELECT l.*
  FROM lock l JOIN valid_mount vm ON vm.mount_id = l.mount_id
  WHERE l.expires_at IS NULL OR l.expires_at > cast(unixepoch('subsec') * 1000 AS INTEGER);

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
     AND (m.expires_at IS NULL OR m.expires_at > cast(unixepoch('subsec') * 1000 AS INTEGER))
     AND (l.expires_at IS NULL OR l.expires_at > cast(unixepoch('subsec') * 1000 AS INTEGER))
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

-- ----------------------------------------------------------------------------
-- Insert-views + INSTEAD OF triggers (id + created_at generation)
-- ----------------------------------------------------------------------------

-- Stateless uuid7 is inlined per-trigger rather than via the uuid7 view, so
-- generation is one statement. The monotonic node/edge path computes the id
-- from the volume's freshly-advanced watermark instead.

-- VOLUME ---------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS volume_new AS
  SELECT volume_id, root_node_id, name, created_at, api_version, chunk_size, enc_mode FROM volume WHERE 0;

CREATE TRIGGER IF NOT EXISTS volume_new_ins INSTEAD OF INSERT ON volume_new
BEGIN
  INSERT INTO volume (volume_id, root_node_id, name, created_at, api_version, chunk_size, wm_ts, wm_seq, enc_mode)
  VALUES (
    coalesce(NEW.volume_id, lower(printf('%s-%s-7%s-%s-%s',
      substr(printf('%012x', cast(unixepoch('subsec') * 1000 AS INTEGER)), 1, 8),
      substr(printf('%012x', cast(unixepoch('subsec') * 1000 AS INTEGER)), 9, 4),
      substr(lower(hex(randomblob(2))), 2, 3),
      substr('89ab', abs(random()) % 4 + 1, 1) || substr(lower(hex(randomblob(2))), 2, 3),
      lower(hex(randomblob(6)))))),
    NEW.root_node_id, NEW.name,
    coalesce(NEW.created_at, cast(unixepoch('subsec') * 1000 AS INTEGER)),
    coalesce(NEW.api_version, 1),
    coalesce(NEW.chunk_size, 1048576),
    0, 0,
    coalesce(NEW.enc_mode, 'none'));
END;

-- NODE -----------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS node_new AS
  SELECT node_id, type, name, created_at, modified_at, volume_id, metadata FROM node WHERE 0;

-- With a volume: monotonic id from the volume watermark.
CREATE TRIGGER IF NOT EXISTS node_new_ins_vol INSTEAD OF INSERT ON node_new
WHEN NEW.volume_id IS NOT NULL
BEGIN
  -- advance the watermark once (borrow 1ms on 12-bit counter overflow)
  UPDATE volume SET wm_ts = c.nts, wm_seq = c.nseq
  FROM (
    WITH n (now) AS (SELECT cast(unixepoch('subsec') * 1000 AS INTEGER)),
         cur AS (SELECT wm_ts AS t, wm_seq AS s FROM volume WHERE volume_id = NEW.volume_id)
    SELECT
      CASE WHEN max(n.now, cur.t) = cur.t AND cur.s + 1 >= 4096
           THEN cur.t + 1 ELSE max(n.now, cur.t) END AS nts,
      CASE WHEN max(n.now, cur.t) = cur.t
           THEN (CASE WHEN cur.s + 1 >= 4096 THEN 0 ELSE cur.s + 1 END)
           ELSE 0 END AS nseq
    FROM n, cur
  ) AS c
  WHERE volume_id = NEW.volume_id;

  -- mint id from the just-advanced watermark
  INSERT INTO node (node_id, type, name, created_at, modified_at, volume_id, metadata)
  SELECT
    lower(printf('%s-%s-7%s-%s-%s',
      substr(v.t, 1, 8), substr(v.t, 9, 4), printf('%03x', v.s),
      substr('89ab', abs(random()) % 4 + 1, 1) || substr(lower(hex(randomblob(2))), 2, 3),
      lower(hex(randomblob(6))))),
    NEW.type, NEW.name,
    coalesce(NEW.created_at, cast(unixepoch('subsec') * 1000 AS INTEGER)),
    coalesce(NEW.modified_at, NEW.created_at, cast(unixepoch('subsec') * 1000 AS INTEGER)),
    NEW.volume_id, NEW.metadata
  FROM (SELECT printf('%012x', wm_ts) AS t, wm_seq AS s FROM volume WHERE volume_id = NEW.volume_id) v;
END;

-- Without a volume (import/recovery): stateless, non-monotonic; honors an
-- explicit node_id if supplied.
CREATE TRIGGER IF NOT EXISTS node_new_ins_novol INSTEAD OF INSERT ON node_new
WHEN NEW.volume_id IS NULL
BEGIN
  INSERT INTO node (node_id, type, name, created_at, modified_at, volume_id, metadata)
  VALUES (
    coalesce(NEW.node_id, lower(printf('%s-%s-7%s-%s-%s',
      substr(printf('%012x', cast(unixepoch('subsec') * 1000 AS INTEGER)), 1, 8),
      substr(printf('%012x', cast(unixepoch('subsec') * 1000 AS INTEGER)), 9, 4),
      substr(lower(hex(randomblob(2))), 2, 3),
      substr('89ab', abs(random()) % 4 + 1, 1) || substr(lower(hex(randomblob(2))), 2, 3),
      lower(hex(randomblob(6)))))),
    NEW.type, NEW.name,
    coalesce(NEW.created_at, cast(unixepoch('subsec') * 1000 AS INTEGER)),
    coalesce(NEW.modified_at, NEW.created_at, cast(unixepoch('subsec') * 1000 AS INTEGER)),
    NULL, NEW.metadata);
END;

-- EDGE -----------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS edge_new AS
  SELECT edge_id, from_id, to_id, volume_id, archived FROM edge WHERE 0;

CREATE TRIGGER IF NOT EXISTS edge_new_ins INSTEAD OF INSERT ON edge_new
BEGIN
  UPDATE volume SET wm_ts = c.nts, wm_seq = c.nseq
  FROM (
    WITH n (now) AS (SELECT cast(unixepoch('subsec') * 1000 AS INTEGER)),
         cur AS (SELECT wm_ts AS t, wm_seq AS s FROM volume WHERE volume_id = NEW.volume_id)
    SELECT
      CASE WHEN max(n.now, cur.t) = cur.t AND cur.s + 1 >= 4096
           THEN cur.t + 1 ELSE max(n.now, cur.t) END AS nts,
      CASE WHEN max(n.now, cur.t) = cur.t
           THEN (CASE WHEN cur.s + 1 >= 4096 THEN 0 ELSE cur.s + 1 END)
           ELSE 0 END AS nseq
    FROM n, cur
  ) AS c
  WHERE volume_id = NEW.volume_id;

  INSERT INTO edge (edge_id, from_id, to_id, volume_id, archived)
  SELECT
    lower(printf('%s-%s-7%s-%s-%s',
      substr(v.t, 1, 8), substr(v.t, 9, 4), printf('%03x', v.s),
      substr('89ab', abs(random()) % 4 + 1, 1) || substr(lower(hex(randomblob(2))), 2, 3),
      lower(hex(randomblob(6))))),
    NEW.from_id, NEW.to_id, NEW.volume_id, coalesce(NEW.archived, 0)
  FROM (SELECT printf('%012x', wm_ts) AS t, wm_seq AS s FROM volume WHERE volume_id = NEW.volume_id) v;
END;

-- MOUNT ----------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS mount_new AS
  SELECT mount_id, volume_id, mount_point, state, expires_at, created_at, N_m FROM mount WHERE 0;

CREATE TRIGGER IF NOT EXISTS mount_new_ins INSTEAD OF INSERT ON mount_new
BEGIN
  INSERT INTO mount (mount_id, volume_id, mount_point, state, expires_at, created_at, N_m)
  VALUES (
    coalesce(NEW.mount_id, lower(printf('%s-%s-7%s-%s-%s',
      substr(printf('%012x', cast(unixepoch('subsec') * 1000 AS INTEGER)), 1, 8),
      substr(printf('%012x', cast(unixepoch('subsec') * 1000 AS INTEGER)), 9, 4),
      substr(lower(hex(randomblob(2))), 2, 3),
      substr('89ab', abs(random()) % 4 + 1, 1) || substr(lower(hex(randomblob(2))), 2, 3),
      lower(hex(randomblob(6)))))),
    NEW.volume_id, NEW.mount_point, coalesce(NEW.state, 'new'), NEW.expires_at,
    coalesce(NEW.created_at, cast(unixepoch('subsec') * 1000 AS INTEGER)),
    coalesce(NEW.N_m, randomblob(16)));
END;

-- LOCK -----------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS lock_new AS
  SELECT lock_id, mount_id, node_id, read_count, write_count, expires_at, created_at FROM lock WHERE 0;

CREATE TRIGGER IF NOT EXISTS lock_new_ins INSTEAD OF INSERT ON lock_new
BEGIN
  INSERT INTO lock (lock_id, mount_id, node_id, read_count, write_count, expires_at, created_at)
  VALUES (
    coalesce(NEW.lock_id, lower(printf('%s-%s-7%s-%s-%s',
      substr(printf('%012x', cast(unixepoch('subsec') * 1000 AS INTEGER)), 1, 8),
      substr(printf('%012x', cast(unixepoch('subsec') * 1000 AS INTEGER)), 9, 4),
      substr(lower(hex(randomblob(2))), 2, 3),
      substr('89ab', abs(random()) % 4 + 1, 1) || substr(lower(hex(randomblob(2))), 2, 3),
      lower(hex(randomblob(6)))))),
    NEW.mount_id, NEW.node_id,
    coalesce(NEW.read_count, 0), coalesce(NEW.write_count, 0),
    NEW.expires_at, coalesce(NEW.created_at, cast(unixepoch('subsec') * 1000 AS INTEGER)));
END;