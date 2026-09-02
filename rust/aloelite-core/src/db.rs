//! Connection + template scaffolding, and the transaction boundary.
//!
//! The substrate the whole flat operation layer is written in terms of. Two
//! responsibilities:
//!
//!   1. Own ONE connection per engine handle (the connection-owning model;
//!      ACC-1 "access is never ambient"). No pool — the single-writer reality
//!      is simply true, not worked around.
//!   2. Execute the SQL templates with named binds, and provide the two
//!      primitives the templates cannot express alone:
//!        * [`Db::create_monotonic`] — host-mints a monotonic id (D-1/D-2),
//!          passes it to the create template as `:id`, and tracks the volume
//!          high-water mark for flush at commit.
//!        * [`Db::txn`] — the transaction boundary that makes the interface's
//!          `atomic` annotations real: commit on `Ok`, rollback on `Err`.
//!
//! The connection arrives OPENED (D-7): `aloelite-store` decides what a
//! database is on each platform — a file, an OPFS handle, a memory image
//! rehydrated from a blob store — and this module never asks. Likewise time
//! and randomness come through [`crate::platform`].

use std::collections::HashMap;
use std::time::Duration;

use rusqlite::types::{FromSql, ToSql};
use rusqlite::{Connection, OptionalExtension, Row, named_params, params};
use zeroize::Zeroizing;

use crate::content::{chunk_hash, split_chunks};
use crate::crypto::{Cipher, EncMode, MOUNT_NONCE_LEN, NONCE_LEN, TOKEN_LEN};
use crate::errors::{FsError, Result};
use crate::ids::{MonotonicMint, stateless_uuid7};
use crate::platform::{Clock, CryptoRngCore};
use crate::templates::SCHEMA_SQL;
use crate::templates::mutation::{INSERT_CHUNK_REF, NEXT_VERSION, UPSERT_CHUNK};
use crate::templates::resolution::{
    GET_CONTENT_META, READ_CHUNK_SIZE, READ_CHUNKS, READ_CHUNKS_RANGE,
};
use crate::types::{MountId, NodeId, VolumeId};

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// The newest SQLite feature the schema relies on is `jsonb()` (3.45);
/// `unixepoch('subsec')` (3.42) has the nastier failure mode — an unknown
/// modifier returns NULL instead of raising. Both are PROBED at open, not
/// parsed from the version string: version parsing lies (vendor backports),
/// capabilities do not.
pub const MIN_SQLITE: (u32, u32) = (3, 45);

/// Schema era stamped into `PRAGMA user_version`. A file stamped OLDER gets
/// its derived objects (views, triggers — no data) dropped and re-created
/// from the current schema on open, after any table-shape migration in
/// [`MIGRATIONS`]. A file stamped NEWER is refused rather than half-read.
/// Bump whenever `schema.sql` changes any view, trigger, or table.
pub const SCHEMA_ERA: i64 = 2;

/// Second writers wait this long before failing (multi-connection model:
/// a mount is a row, not a connection).
pub const BUSY_TIMEOUT_MS: u64 = 5000;

/// ms epochs stay below 1e15 until the year 33658; ns epochs passed 1e18 in
/// 2001. A stored value under this bound is an unmigrated millisecond value,
/// which is what makes the ×1e6 rewrite safe to rerun after a crash.
const NS_BOUND: i64 = 1_000_000_000_000_000;

/// Table-shape migrations, keyed by the era they upgrade a file TO. Each runs
/// before the derived-object rebuild and must be crash-idempotent: a failure
/// between migration and stamp reruns it.
const MIGRATIONS: &[(i64, Migration)] = &[(2, migrate_to_era2)];

/// One table-shape upgrade step, run against the raw connection.
type Migration = fn(&Connection) -> rusqlite::Result<()>;

/// Owns one connection and runs templates against it.
pub struct Db {
    conn: Connection,
    /// The at-rest cipher for the mounted session. Identity by default, so an
    /// unencrypted volume runs the same path; `mount` installs a chunk cipher
    /// when a PIN unlocks an encrypted volume, `unmount` restores identity.
    pub(crate) cipher: Cipher,
    /// Per-mount session material (runtime only): the token handed to the
    /// user, the mount nonce, and the memory-only sealed mount secret. Never
    /// persisted beyond `N_m` (which is on the mount row).
    pub(crate) session: Option<Session>,
    mints: HashMap<VolumeId, MonotonicMint>,
    pending_wm: HashMap<VolumeId, (u64, u16)>,
    clock: Box<dyn Clock>,
    rng: Box<dyn CryptoRngCore + Send>,
}

/// What `mount` leaves in memory after unlocking an encrypted volume.
pub struct Session {
    pub mount_id: MountId,
    pub volume: VolumeId,
    pub enc_mode: EncMode,
    /// The per-mount token `T`, which with `N_m` recovers `K_v` without the
    /// PIN. A wrapper surfaces it as the mount's token.
    pub token: Zeroizing<[u8; TOKEN_LEN]>,
    pub mount_nonce: [u8; MOUNT_NONCE_LEN],
    pub mount_secret: Zeroizing<Vec<u8>>,
    pub session_nonce: [u8; NONCE_LEN],
}

impl Db {
    /// Take ownership of an opened connection: probe capabilities, set the
    /// connection pragmas, gate on the schema era, migrate, install the
    /// schema, stamp.
    pub fn open(
        conn: Connection,
        clock: impl Clock + 'static,
        rng: impl CryptoRngCore + Send + 'static,
    ) -> Result<Db> {
        check_sqlite_capabilities(&conn)?;
        configure_connection(&conn)?;
        install_schema(&conn)?;
        Ok(Db {
            conn,
            cipher: Cipher::identity(),
            session: None,
            mints: HashMap::new(),
            pending_wm: HashMap::new(),
            clock: Box::new(clock),
            rng: Box::new(rng),
        })
    }

    /// Flush any advance the mint made outside a transaction, then close.
    pub fn close(mut self) -> Result<()> {
        if !self.pending_wm.is_empty() && self.conn.is_autocommit() {
            // best effort: the detach fence still bounds the next session (D-2)
            let _ = self.flush_watermarks();
        }
        self.conn.close().map_err(|(_, e)| e.into())
    }

    /// Escape hatch for the few host-only walks (and storage inspections)
    /// that need the raw connection.
    pub fn connection(&self) -> &Connection {
        &self.conn
    }

    pub fn cipher(&self) -> &Cipher {
        &self.cipher
    }

    pub fn session(&self) -> Option<&Session> {
        self.session.as_ref()
    }

    /// The host clock, as era-2 nanoseconds.
    pub fn now_ns(&self) -> i64 {
        self.clock.now_ns()
    }

    /// The host entropy source, for the key-ladder steps the session
    /// operations perform.
    pub(crate) fn rng(&mut self) -> &mut (dyn CryptoRngCore + Send) {
        &mut *self.rng
    }

    // -- raw template execution ---------------------------------------------

    /// Execute a template for effect; returns the number of rows changed.
    pub fn run(&self, sql: &str, params: &[(&str, &dyn ToSql)]) -> Result<usize> {
        let mut st = self.conn.prepare_cached(sql)?;
        Ok(st.execute(params)?)
    }

    /// The first row, mapped, or `None`.
    pub fn one<T>(
        &self,
        sql: &str,
        params: &[(&str, &dyn ToSql)],
        f: impl FnMut(&Row<'_>) -> Result<T>,
    ) -> Result<Option<T>> {
        let mut st = self.conn.prepare_cached(sql)?;
        let mut rows = st.query_and_then(params, f)?;
        match rows.next() {
            Some(row) => Ok(Some(row?)),
            None => Ok(None),
        }
    }

    /// Every row, mapped, in statement order.
    pub fn all<T>(
        &self,
        sql: &str,
        params: &[(&str, &dyn ToSql)],
        f: impl FnMut(&Row<'_>) -> Result<T>,
    ) -> Result<Vec<T>> {
        let mut st = self.conn.prepare_cached(sql)?;
        let rows = st.query_and_then(params, f)?;
        rows.collect()
    }

    /// The first column of the first row, or `None` when there is no row.
    pub fn scalar<T: FromSql>(
        &self,
        sql: &str,
        params: &[(&str, &dyn ToSql)],
    ) -> Result<Option<T>> {
        self.one(sql, params, |r| Ok(r.get::<_, T>(0)?))
    }

    // -- id generation ------------------------------------------------------
    //
    // Host-minted (D-1/D-2). The caller always holds the id before the
    // INSERT, so there is no read-back and no single-owning-connection
    // requirement.
    //   * node / edge ids come from a per-volume MonotonicMint, fenced at
    //     first use by the volume's stored (wm_ts, wm_seq) high-water mark, so
    //     a new session can never mint at or below anything the volume has
    //     recorded — clock regression and failover skew included.
    //   * volume / mount / lock ids are stateless uuid7s (no ordering promise).
    // The high-water mark is written back inside the same transaction as the
    // creates it covers (one monotonic UPDATE per write txn, not per id), so a
    // rollback loses the advance together with the rows.

    /// A fresh stateless uuid7 (volume / mount / lock / descriptor ids).
    pub fn gen_id(&mut self) -> String {
        let ms = self.now_ms();
        stateless_uuid7(ms, &mut self.rng)
    }

    /// Create a node/edge with a host-minted monotonic id, passed to the
    /// template as `:id`. `volume` is `None` only on the import/recovery
    /// path — stateless mint, no watermark.
    pub fn create_monotonic(
        &mut self,
        sql: &str,
        volume: Option<&VolumeId>,
        params: &[(&str, &dyn ToSql)],
    ) -> Result<String> {
        let now_ms = self.now_ms();
        let new_id = match volume {
            None => stateless_uuid7(now_ms, &mut self.rng),
            Some(volume) => {
                self.fence_mint(volume)?;
                let mint = self.mints.get_mut(volume).expect("fenced above");
                let id = mint.mint(now_ms, &mut self.rng);
                let state = mint.state().expect("a mint that just minted has state");
                self.pending_wm.insert(volume.clone(), state);
                id
            }
        };
        let mut bound: Vec<(&str, &dyn ToSql)> = params.to_vec();
        bound.push((":id", &new_id));
        self.run(sql, &bound)?;
        if self.conn.is_autocommit() {
            self.flush_watermarks()?;
        }
        Ok(new_id)
    }

    // -- transaction boundary -----------------------------------------------

    /// Atomic boundary for one operation: commit on `Ok`, roll back on `Err`.
    /// Not nestable (operations are flat); one call wraps one whole Mount API
    /// operation.
    pub fn txn<T>(&mut self, f: impl FnOnce(&mut Db) -> Result<T>) -> Result<T> {
        self.conn.execute_batch("BEGIN")?;
        match f(self) {
            Ok(value) => {
                if let Err(e) = self
                    .flush_watermarks()
                    .and_then(|()| Ok(self.conn.execute_batch("COMMIT")?))
                {
                    self.pending_wm.clear();
                    let _ = self.conn.execute_batch("ROLLBACK");
                    return Err(e);
                }
                Ok(value)
            }
            Err(e) => {
                // discard the advance with the rows it covered
                self.pending_wm.clear();
                let _ = self.conn.execute_batch("ROLLBACK");
                Err(e)
            }
        }
    }

    // -- content chunking primitives ----------------------------------------
    //
    // Shared by the operations and the streaming descriptor so both chunk and
    // reassemble identically. They run inside whatever txn the caller has
    // open: the atomic whole-file ops inside the op's single transaction; the
    // streaming writer inside its own short per-chunk commits (CV-5).

    /// The volume's fixed chunk size (CV-1), read from the volume row.
    pub fn chunk_size_of(&self, volume: &VolumeId) -> Result<usize> {
        let cs: Option<i64> = self.scalar(READ_CHUNK_SIZE, named_params! { ":volume": volume })?;
        match cs {
            Some(cs) if cs > 0 => Ok(cs as usize),
            _ => Err(FsError::corrupt(format!(
                "volume {volume} has no chunk size"
            ))),
        }
    }

    /// The next per-content version to write (CV-3), allocated under the
    /// entry's write lock. Held by the caller; this is just the read.
    pub fn alloc_version(&self, node: &NodeId) -> Result<i64> {
        self.scalar(NEXT_VERSION, named_params! { ":node": node })?
            .ok_or_else(|| FsError::internal("next_version returned no row"))
    }

    /// Split `data`, upsert each chunk into the immutable pool (dedup), and
    /// record the ordered manifest rows for (node, version). Returns the
    /// total byte size. Does NOT advance the committed pointer — that is the
    /// separate swap (`update_content`). Uniform chunking: a tiny file is one
    /// short chunk; an empty payload stages zero chunks.
    pub fn stage_chunks(
        &mut self,
        node: &NodeId,
        version: i64,
        volume: &VolumeId,
        data: &[u8],
    ) -> Result<usize> {
        let cs = self.chunk_size_of(volume)?;
        for (index, chunk) in split_chunks(data, cs).into_iter().enumerate() {
            self.stage_chunk(node, version, index as i64, chunk)?;
        }
        Ok(data.len())
    }

    /// Stage ONE chunk plus its single ordered manifest ref, in the caller's
    /// txn. The address is computed over the CIPHERTEXT actually stored, so
    /// "same address ⇔ same stored bytes" holds even across volumes keyed
    /// differently.
    pub fn stage_chunk(
        &mut self,
        node: &NodeId,
        version: i64,
        index: i64,
        data: &[u8],
    ) -> Result<()> {
        let sealed = self.cipher.encrypt_chunk(data, &mut self.rng);
        let hash = chunk_hash(&sealed.ciphertext);
        self.run(
            UPSERT_CHUNK,
            named_params! {
                ":hash": hash,
                ":data": sealed.ciphertext,
                ":length": data.len() as i64,
                ":n_c": sealed.nonce,
                ":tag": sealed.tag,
            },
        )?;
        self.run(
            INSERT_CHUNK_REF,
            named_params! { ":node": node, ":version": version, ":index": index, ":hash": hash },
        )?;
        Ok(())
    }

    /// (committed version, materialized size) for a leaf, or `None` if it has
    /// no content row.
    pub fn read_content_meta(&self, node: &NodeId) -> Result<Option<(i64, i64)>> {
        self.one(GET_CONTENT_META, named_params! { ":node": node }, |r| {
            Ok((r.get("version")?, r.get("size")?))
        })
    }

    /// The chunks of `version` whose index is in `[lo, hi]`, decrypted, in
    /// order. The streaming reader fetches only the chunks covering a
    /// requested byte range instead of reassembling the whole file.
    pub fn read_chunk_range(
        &self,
        node: &NodeId,
        version: i64,
        lo: i64,
        hi: i64,
    ) -> Result<Vec<(i64, Vec<u8>)>> {
        let rows = self.all(
            READ_CHUNKS_RANGE,
            named_params! { ":node": node, ":version": version, ":lo": lo, ":hi": hi },
            |r| Ok((r.get::<_, i64>("chunk_index")?, StoredChunk::from_row(r)?)),
        )?;
        rows.into_iter()
            .map(|(index, chunk)| {
                Ok((
                    index,
                    self.cipher
                        .decrypt_chunk(&chunk.data, &chunk.nonce, &chunk.tag)?,
                ))
            })
            .collect()
    }

    /// Reassemble a leaf's current bytes from its committed version's ordered
    /// chunk manifest. No content row or zero chunks ⇒ empty.
    pub fn read_content_bytes(&self, node: &NodeId) -> Result<Vec<u8>> {
        let Some((version, _)) = self.read_content_meta(node)? else {
            return Ok(Vec::new());
        };
        let rows = self.all(
            READ_CHUNKS,
            named_params! { ":node": node, ":version": version },
            StoredChunk::from_row,
        )?;
        let mut out = Vec::new();
        for chunk in rows {
            out.extend_from_slice(&self.cipher.decrypt_chunk(
                &chunk.data,
                &chunk.nonce,
                &chunk.tag,
            )?);
        }
        Ok(out)
    }
}

// ---------------------------------------------------------------------------
// depth: open-time checks, pragmas, era gate and migration
// ---------------------------------------------------------------------------

struct StoredChunk {
    data: Vec<u8>,
    nonce: Vec<u8>,
    tag: Vec<u8>,
}

impl StoredChunk {
    fn from_row(r: &Row<'_>) -> Result<Self> {
        Ok(StoredChunk {
            data: r.get("data")?,
            nonce: r.get("N_c")?,
            tag: r.get("enc_tag")?,
        })
    }
}

/// Refuse a too-old SQLite AT OPEN with an actionable message, instead of
/// letting it surface later as a NOT NULL violation or a wrong-answer NULL
/// timestamp.
fn check_sqlite_capabilities(conn: &Connection) -> Result<()> {
    let ok = conn
        .query_row("SELECT jsonb(1)", [], |_| Ok(()))
        .and_then(|()| {
            conn.query_row("SELECT unixepoch('subsec') IS NOT NULL", [], |r| {
                r.get::<_, i64>(0)
            })
        })
        .map(|subsec_ok| subsec_ok != 0)
        .unwrap_or(false);
    if ok {
        return Ok(());
    }
    let (major, minor) = MIN_SQLITE;
    Err(FsError::unsupported(format!(
        "host sqlite {} is too old for aloelite (needs jsonb + unixepoch subsec, sqlite >= {major}.{minor})",
        rusqlite::version()
    )))
}

fn configure_connection(conn: &Connection) -> Result<()> {
    conn.execute_batch("PRAGMA foreign_keys = ON")?;
    // PROBE THE RESULT, do not trust an error: `PRAGMA journal_mode` RETURNS
    // the resulting mode, and a request it cannot honor comes back as the
    // unchanged mode with no error (':memory:' answers 'memory'). WAL needs
    // shared memory, which network filesystems (and some VFSes) refuse; the
    // fallback is PERSIST, which avoids journal unlink churn.
    let mode: String = conn
        .query_row("PRAGMA journal_mode = WAL", [], |r| r.get(0))
        .unwrap_or_else(|_| "refused".to_owned());
    if !matches!(mode.to_ascii_lowercase().as_str(), "wal" | "memory") {
        conn.execute_batch("PRAGMA journal_mode = PERSIST")?;
    }
    conn.busy_timeout(Duration::from_millis(BUSY_TIMEOUT_MS))?;
    Ok(())
}

fn install_schema(conn: &Connection) -> Result<()> {
    let era: i64 = conn.query_row("PRAGMA user_version", [], |r| r.get(0))?;
    if era > SCHEMA_ERA {
        return Err(FsError::unsupported(format!(
            "file was written by a newer aloelite (schema era {era}; this build understands {SCHEMA_ERA}). Upgrade aloelite to open it."
        )));
    }
    let fresh = conn
        .query_row(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1",
            [],
            |_| Ok(()),
        )
        .optional()?
        .is_none();
    if era < SCHEMA_ERA {
        // Table-shape migrations first, oldest era to newest, so the derived
        // objects rebuilt below can reference the new columns. A fresh file
        // has no tables to migrate — the schema script creates them
        // era-current.
        if !fresh {
            for (target, step) in MIGRATIONS {
                if *target > era {
                    step(conn)?;
                }
            }
        }
        // Derived objects belong to the installed version, not to the file's
        // creation era: drop every trigger and view so the schema script
        // re-creates them from the CURRENT definitions. They hold no data;
        // tables keep IF NOT EXISTS and are never dropped here.
        let derived: Vec<(String, String)> = {
            let mut st = conn.prepare(
                "SELECT type, name FROM sqlite_master WHERE type IN ('trigger', 'view')",
            )?;
            let rows = st.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?;
            rows.collect::<rusqlite::Result<_>>()?
        };
        for (kind, name) in derived.iter().filter(|(k, _)| k == "trigger") {
            debug_assert_eq!(kind, "trigger");
            conn.execute_batch(&format!("DROP TRIGGER IF EXISTS \"{name}\""))?;
        }
        for (_, name) in derived.iter().filter(|(k, _)| k == "view") {
            conn.execute_batch(&format!("DROP VIEW IF EXISTS \"{name}\""))?;
        }
    }
    conn.execute_batch(SCHEMA_SQL)?;
    conn.execute_batch(&format!("PRAGMA user_version = {SCHEMA_ERA}"))?;
    Ok(())
}

fn column_exists(conn: &Connection, table: &str, column: &str) -> rusqlite::Result<bool> {
    Ok(conn
        .query_row(
            "SELECT 1 FROM pragma_table_info(?1) WHERE name = ?2",
            params![table, column],
            |_| Ok(()),
        )
        .optional()?
        .is_some())
}

/// Era 1 → 2. Additive columns (guarded per column so a crashed run can
/// rerun), the ms→ns value rewrite (guarded by `NS_BOUND`), and the drop of
/// the era-1 PI-1 partial unique index (its narrowed replacement is the
/// `edge_guard_single_parent` trigger pair, rebuilt with the derived objects).
fn migrate_to_era2(conn: &Connection) -> rusqlite::Result<()> {
    for (table, column, decl) in [
        ("node", "uid", "INTEGER"),
        ("node", "gid", "INTEGER"),
        ("node", "mode", "INTEGER"),
        ("node", "atime", "INTEGER"),
        ("node", "ctime", "INTEGER"),
        ("edge", "name", "TEXT"),
        ("mount", "access", "TEXT NOT NULL DEFAULT 'rw'"),
        ("mount", "principal", "TEXT"),
    ] {
        if !column_exists(conn, table, column)? {
            conn.execute_batch(&format!(
                "ALTER TABLE \"{table}\" ADD COLUMN \"{column}\" {decl}"
            ))?;
        }
    }
    for (table, column) in [
        ("node", "created_at"),
        ("node", "modified_at"),
        ("volume", "created_at"),
        ("mount", "created_at"),
        ("mount", "expires_at"),
        ("lock", "created_at"),
        ("lock", "expires_at"),
    ] {
        conn.execute_batch(&format!(
            "UPDATE \"{table}\" SET \"{column}\" = \"{column}\" * 1000000 \
             WHERE \"{column}\" IS NOT NULL AND \"{column}\" < {NS_BOUND} AND \"{column}\" > 0"
        ))?;
    }
    conn.execute_batch("DROP INDEX IF EXISTS edge_active_placement")?;
    Ok(())
}

// ---------------------------------------------------------------------------
// depth: the mint and its watermark
// ---------------------------------------------------------------------------

impl Db {
    fn now_ms(&self) -> u64 {
        (self.now_ns().max(0) / 1_000_000) as u64
    }

    /// Make sure `volume` has a mint, fenced from the volume's stored
    /// high-water mark on first use.
    fn fence_mint(&mut self, volume: &VolumeId) -> Result<()> {
        if self.mints.contains_key(volume) {
            return Ok(());
        }
        let mut mint = MonotonicMint::new();
        let mark: Option<(i64, i64)> = self
            .conn
            .query_row(
                "SELECT wm_ts, wm_seq FROM volume WHERE volume_id = ?1",
                params![volume],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()?;
        if let Some((ts, seq)) = mark {
            mint.fence(ts.max(0) as u64, seq.clamp(0, i64::from(u16::MAX)) as u16);
        }
        self.mints.insert(volume.clone(), mint);
        Ok(())
    }

    /// Advance each touched volume's high-water mark to the mint's state. The
    /// monotonic guard in the WHERE lets concurrent sessions interleave
    /// flushes in any order without ever moving a mark backwards.
    fn flush_watermarks(&mut self) -> Result<()> {
        for (volume, (ts, seq)) in self.pending_wm.drain() {
            self.conn.execute(
                "UPDATE volume SET wm_ts = ?1, wm_seq = ?2 WHERE volume_id = ?3 \
                 AND (wm_ts < ?1 OR (wm_ts = ?1 AND wm_seq < ?2))",
                params![ts as i64, i64::from(seq), volume],
            )?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::platform::FixedClock;

    struct Zeros;
    impl rand_core::TryRng for Zeros {
        type Error = rand_core::Infallible;
        fn try_next_u32(&mut self) -> std::result::Result<u32, Self::Error> {
            Ok(0)
        }
        fn try_next_u64(&mut self) -> std::result::Result<u64, Self::Error> {
            Ok(0)
        }
        fn try_fill_bytes(&mut self, dst: &mut [u8]) -> std::result::Result<(), Self::Error> {
            dst.fill(0);
            Ok(())
        }
    }
    impl rand_core::TryCryptoRng for Zeros {}

    fn open() -> Db {
        Db::open(
            Connection::open_in_memory().unwrap(),
            FixedClock(1_700_000_000_000_000_000),
            Zeros,
        )
        .unwrap()
    }

    #[test]
    fn a_fresh_file_is_stamped_with_the_current_era_and_has_the_schema() {
        let db = open();
        let era: i64 = db
            .connection()
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(era, SCHEMA_ERA);
        let tables: i64 = db
            .connection()
            .query_row(
                "SELECT count(*) FROM sqlite_master WHERE type = 'table'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(
            tables, 9,
            "node, content, content_chunk, content_version, volume, edge, mount, lock, xattr"
        );
    }

    #[test]
    fn a_newer_era_is_refused_rather_than_half_read() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch("PRAGMA user_version = 99").unwrap();
        let err = Db::open(conn, FixedClock(0), Zeros).err().expect("refused");
        assert_eq!(err.code(), Some("unsupported"));
    }

    #[test]
    fn reopening_refreshes_derived_objects_without_touching_tables() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch("CREATE TABLE node (node_id TEXT PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL, created_at INTEGER NOT NULL, modified_at INTEGER, volume_id TEXT, metadata BLOB) STRICT;
                            CREATE TABLE volume (volume_id TEXT PRIMARY KEY, root_node_id TEXT UNIQUE, name TEXT, created_at INTEGER NOT NULL, api_version INTEGER NOT NULL DEFAULT 1, chunk_size INTEGER NOT NULL DEFAULT 1048576, wm_ts INTEGER NOT NULL DEFAULT 0, wm_seq INTEGER NOT NULL DEFAULT 0, enc_mode TEXT NOT NULL DEFAULT 'none', wrapped_key BLOB, wrap_nonce BLOB) STRICT;
                            CREATE TABLE edge (edge_id TEXT PRIMARY KEY, from_id TEXT NOT NULL, to_id TEXT NOT NULL, volume_id TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0) STRICT;
                            CREATE TABLE mount (mount_id TEXT PRIMARY KEY, volume_id TEXT NOT NULL, mount_point TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'new', expires_at INTEGER, created_at INTEGER NOT NULL, N_m BLOB NOT NULL) STRICT;
                            CREATE TABLE lock (lock_id TEXT PRIMARY KEY, mount_id TEXT NOT NULL, node_id TEXT NOT NULL, read_count INTEGER NOT NULL DEFAULT 0, write_count INTEGER NOT NULL DEFAULT 0, expires_at INTEGER, created_at INTEGER NOT NULL) STRICT;
                            INSERT INTO node VALUES ('n1', 'container', '/', 1700000000000, NULL, NULL, NULL);
                            CREATE VIEW stale AS SELECT 1;
                            PRAGMA user_version = 1;").unwrap();
        let db = Db::open(conn, FixedClock(0), Zeros).unwrap();
        // era-1 milliseconds became nanoseconds, the added columns exist, the stale view is gone
        let created: i64 = db
            .connection()
            .query_row(
                "SELECT created_at FROM node WHERE node_id = 'n1'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(created, 1_700_000_000_000_000_000);
        assert!(column_exists(db.connection(), "edge", "name").unwrap());
        let stale: Option<()> = db
            .connection()
            .query_row(
                "SELECT 1 FROM sqlite_master WHERE name = 'stale'",
                [],
                |_| Ok(()),
            )
            .optional()
            .unwrap();
        assert!(stale.is_none());
        let era: i64 = db
            .connection()
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(era, SCHEMA_ERA);
    }

    #[test]
    fn a_rolled_back_transaction_discards_the_watermark_advance() {
        let mut db = open();
        let vid = VolumeId("v".into());
        db.run(
            "INSERT INTO volume (volume_id, created_at) VALUES (:id, 1)",
            named_params! { ":id": vid },
        )
        .unwrap();
        let err = db.txn(|db| {
            db.create_monotonic(
                "INSERT INTO node (node_id, type, name, created_at, volume_id) VALUES (:id, 'container', 'x', 1, :volume)",
                Some(&vid),
                named_params! { ":volume": vid },
            )?;
            Err::<(), _>(FsError::internal("abort"))
        });
        assert!(err.is_err());
        let (ts, seq): (i64, i64) = db
            .connection()
            .query_row(
                "SELECT wm_ts, wm_seq FROM volume WHERE volume_id = 'v'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!((ts, seq), (0, 0));
        // and a committed one advances it
        db.txn(|db| {
            db.create_monotonic(
                "INSERT INTO node (node_id, type, name, created_at, volume_id) VALUES (:id, 'container', 'x', 1, :volume)",
                Some(&vid),
                named_params! { ":volume": vid },
            )
        })
        .unwrap();
        let (ts, _): (i64, i64) = db
            .connection()
            .query_row(
                "SELECT wm_ts, wm_seq FROM volume WHERE volume_id = 'v'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(ts, 1_700_000_000_000);
    }
}
