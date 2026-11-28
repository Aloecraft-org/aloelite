//! Maintenance: prune, prune_content, verify, health_check.

use std::collections::{HashMap, HashSet};

use rusqlite::named_params;

use crate::content::chunk_hash;
use crate::db::Db;
use crate::errors::Result;
use crate::records::{Anomaly, ContentPruneReport, PruneReport, VerifyReport};
use crate::templates::maintenance::{
    HEALTH_CHECK, PRUNE_CONTENT_CHUNKS, PRUNE_CONTENT_COMMITS, PRUNE_CONTENT_VERSIONS,
    PRUNE_DELETED_EDGES, PRUNE_LOCKS, PRUNE_NODES,
};
use crate::templates::resolution::{GET_CHUNK, VERIFY_MANIFEST};
use crate::types::{MountId, VolumeId};

use super::require_mount;

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// Lazy sweep: volatile nodes (PI-3), invalid locks (ACC-10), and deleted
/// nodes on volumes whose `retention_on_delete` is 0.
pub fn prune(db: &mut Db, volume: Option<&VolumeId>) -> Result<PruneReport> {
    db.txn(|db| {
        let locks = db.run(PRUNE_LOCKS, &[])?;
        // retention_on_delete = 0: deleted nodes lose their placement records
        // here, which makes them volatile for the sweep on the next line
        db.run(PRUNE_DELETED_EDGES, named_params! { ":volume": volume })?;
        let nodes = db.run(PRUNE_NODES, named_params! { ":volume": volume })?;
        Ok(PruneReport {
            nodes_pruned: nodes,
            locks_pruned: locks,
        })
    })
}

/// Reclaim unreferenced content (CV-7), distinct from `prune`: drop manifest
/// rows beyond each entry's retention policy (and aborted writes above the
/// committed pointer), then sweep pool chunks no retained version references.
/// Retained versions are resolved BEFORE any chunk is collected, and the
/// committed version always survives.
pub fn prune_content(db: &mut Db, volume: Option<&VolumeId>) -> Result<ContentPruneReport> {
    db.txn(|db| {
        let versions = db.run(PRUNE_CONTENT_VERSIONS, named_params! { ":volume": volume })?;
        db.run(PRUNE_CONTENT_COMMITS, named_params! { ":volume": volume })?;
        let chunks = db.run(PRUNE_CONTENT_CHUNKS, &[])?;
        Ok(ContentPruneReport {
            versions_pruned: versions,
            chunks_pruned: chunks,
        })
    })
}

/// Check every leaf's COMMITTED version in the mount's volume.
///
/// Shallow (always): manifest refs resolve to pool rows, chunk indexes are
/// contiguous from 0, and per-entry chunk lengths sum to the manifest size.
/// Deep: additionally fetch each referenced chunk once, recompute its
/// address over the stored bytes (bitrot), and decrypt it under the mount's
/// cipher (AEAD tag ⇒ authenticity). Superseded/staged versions are out of
/// scope — prune owns those.
pub fn verify(db: &mut Db, mount: &MountId, deep: bool) -> Result<VerifyReport> {
    struct Ref {
        node: String,
        version: i64,
        size: i64,
        chunks: Option<i64>,
        chunk_index: Option<i64>,
        chunk_hash: Option<String>,
        chunk_length: Option<i64>,
        present: bool,
    }
    let m = require_mount(db, mount, false)?;
    let rows = db.all(
        VERIFY_MANIFEST,
        named_params! { ":volume": m.volume },
        |r| {
            Ok(Ref {
                node: r.get("node_id")?,
                version: r.get("version")?,
                size: r.get("size")?,
                chunks: r.get("chunks")?,
                chunk_index: r.get("chunk_index")?,
                chunk_hash: r.get("chunk_hash")?,
                chunk_length: r.get("chunk_length")?,
                present: r.get::<_, i64>("present")? != 0,
            })
        },
    )?;

    // group by (node, version), keeping first-seen order
    let mut order: Vec<(String, i64)> = Vec::new();
    let mut groups: HashMap<(String, i64), Vec<&Ref>> = HashMap::new();
    for r in &rows {
        let key = (r.node.clone(), r.version);
        groups
            .entry(key.clone())
            .or_insert_with(|| {
                order.push(key);
                Vec::new()
            })
            .push(r);
    }

    let mut problems = Vec::new();
    let mut seen: Vec<String> = Vec::new();
    let mut seen_set: HashSet<String> = HashSet::new();
    for key in &order {
        let all_refs = &groups[key];
        let (node, version) = key;
        // one row with a NULL chunk_index = a committed version with no
        // manifest row at all (legitimate only when it has zero chunks)
        let refs: Vec<(i64, &str, &Ref)> = all_refs
            .iter()
            .filter_map(|r| Some((r.chunk_index?, r.chunk_hash.as_deref()?, *r)))
            .collect();
        let idxs: Vec<i64> = refs.iter().map(|(i, _, _)| *i).collect();
        match all_refs[0].chunks {
            None => problems.push(format!("no-commit-record node={node} v={version}")),
            Some(chunks) if idxs.iter().copied().ne(0..chunks) => {
                problems.push(format!("gap node={node} v={version} indexes={idxs:?}"));
            }
            Some(_) => {}
        }
        let mut total = 0i64;
        for (_, hash, r) in &refs {
            if !r.present {
                problems.push(format!("missing-chunk node={node} hash={hash}"));
                continue;
            }
            total += r.chunk_length.unwrap_or(0);
            if seen_set.insert((*hash).to_string()) {
                seen.push((*hash).to_string());
            }
        }
        if total != all_refs[0].size {
            problems.push(format!(
                "size-mismatch node={node} manifest={} chunks={total}",
                all_refs[0].size
            ));
        }
    }

    if deep {
        for h in &seen {
            let chunk = db.one(GET_CHUNK, named_params! { ":hash": h }, |r| {
                Ok((
                    r.get::<_, Vec<u8>>("data")?,
                    r.get::<_, i64>("length")?,
                    r.get::<_, Vec<u8>>("N_c")?,
                    r.get::<_, Vec<u8>>("enc_tag")?,
                ))
            })?;
            let Some((data, length, nonce, tag)) = chunk else {
                continue; // already reported as missing
            };
            if chunk_hash(&data) != *h {
                problems.push(format!("address-mismatch hash={h}"));
                continue;
            }
            match db.cipher().decrypt_chunk(&data, &nonce, &tag) {
                Err(_) => problems.push(format!("decrypt-failed hash={h}")),
                Ok(pt) if pt.len() as i64 != length => {
                    problems.push(format!("length-mismatch hash={h}"))
                }
                Ok(_) => {}
            }
        }
    }

    Ok(VerifyReport {
        entries_checked: order.len(),
        chunks_checked: if deep { seen.len() } else { 0 },
        problems,
    })
}

/// Surface `health_anomaly` rows; an empty list means consistent.
pub fn health_check(db: &mut Db) -> Result<Vec<Anomaly>> {
    db.all(HEALTH_CHECK, &[], Anomaly::from_row)
}

// ---------------------------------------------------------------------------
// depth: tests for the volume retention settings and purge, which act at
// prune and so sit outside what a conformance scenario can reach
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use rusqlite::Connection;

    use super::*;
    use crate::crypto::EncMode;
    use crate::db::Db;
    use crate::errors::FsError;
    use crate::ops::{self, MountOptions};
    use crate::platform::FixedClock;
    use crate::types::NodeId;

    /// Deterministic and never repeating, so random ids do not collide.
    struct Counter(u64);
    impl rand_core::TryRng for Counter {
        type Error = rand_core::Infallible;
        fn try_next_u32(&mut self) -> std::result::Result<u32, Self::Error> {
            Ok(self.try_next_u64()? as u32)
        }
        fn try_next_u64(&mut self) -> std::result::Result<u64, Self::Error> {
            self.0 += 1;
            Ok(self.0)
        }
        fn try_fill_bytes(&mut self, dst: &mut [u8]) -> std::result::Result<(), Self::Error> {
            for b in dst {
                self.0 += 1;
                *b = self.0 as u8;
            }
            Ok(())
        }
    }
    impl rand_core::TryCryptoRng for Counter {}

    /// A volume with 4-byte chunks and a mount on its root.
    fn rig() -> (Db, VolumeId, MountId) {
        let mut db = Db::open(
            Connection::open_in_memory().unwrap(),
            FixedClock(1_700_000_000_000_000_000),
            Counter(0),
        )
        .unwrap();
        let vol = ops::create_volume(&mut db, Some("v"), 4, None, EncMode::None).unwrap();
        let mount = ops::mount(&mut db, &vol.id, &MountOptions::default()).unwrap();
        (db, vol.id, mount)
    }

    fn nid(db: &mut Db, mount: &MountId, path: &str) -> NodeId {
        ops::stat(db, mount, path).unwrap().id
    }

    fn versions(db: &Db, node: &NodeId) -> Vec<i64> {
        let mut stmt = db
            .connection()
            .prepare("SELECT version FROM content_commit WHERE content_id = ?1 ORDER BY version")
            .unwrap();
        stmt.query_map([&node.0], |r| r.get(0))
            .unwrap()
            .map(|v| v.unwrap())
            .collect()
    }

    fn node_exists(db: &Db, node: &NodeId) -> bool {
        db.connection()
            .query_row(
                "SELECT count(*) FROM node WHERE node_id = ?1",
                [&node.0],
                |r| r.get::<_, i64>(0),
            )
            .unwrap()
            > 0
    }

    fn rewrite(db: &mut Db, mount: &MountId, path: &str, times: u8) {
        for i in 0..times {
            ops::write_all(db, mount, path, &[65 + i; 4]).unwrap();
        }
    }

    #[test]
    fn the_volume_default_governs_entries_without_their_own_policy() {
        let (mut db, vol, m) = rig();
        ops::create_entry(
            &mut db,
            &m,
            "/plain",
            Some(b"0000"),
            crate::types::Ownership::NONE,
        )
        .unwrap();
        ops::create_entry(
            &mut db,
            &m,
            "/own",
            Some(b"0000"),
            crate::types::Ownership::NONE,
        )
        .unwrap();
        ops::set_retention(&mut db, &m, "/own", Some(3)).unwrap();
        rewrite(&mut db, &m, "/plain", 5);
        rewrite(&mut db, &m, "/own", 5);
        let (plain, own) = (nid(&mut db, &m, "/plain"), nid(&mut db, &m, "/own"));

        // no default: an entry without a policy keeps everything, as before
        ops::prune_content(&mut db, None).unwrap();
        assert_eq!(versions(&db, &plain), [1, 2, 3, 4, 5, 6]);
        assert_eq!(versions(&db, &own), [4, 5, 6]);

        // with one, it applies to the entry that has none and not to the other
        ops::set_default_retention(&mut db, &vol, Some(1)).unwrap();
        let info = ops::list_volumes(&mut db).unwrap().remove(0);
        assert_eq!((info.retention, info.retention_on_delete), (Some(1), 1));
        ops::prune_content(&mut db, None).unwrap();
        assert_eq!(versions(&db, &plain), [6]);
        assert_eq!(versions(&db, &own), [4, 5, 6]);
        assert_eq!(ops::read_all(&mut db, &m, "/plain").unwrap(), b"EEEE");

        // cleared, history accumulates again
        ops::set_default_retention(&mut db, &vol, None).unwrap();
        rewrite(&mut db, &m, "/plain", 2);
        ops::prune_content(&mut db, None).unwrap();
        assert_eq!(versions(&db, &plain), [6, 7, 8]);
    }

    #[test]
    fn no_policy_removes_current_content() {
        let (mut db, _, m) = rig();
        ops::create_entry(
            &mut db,
            &m,
            "/f",
            Some(b"0000"),
            crate::types::Ownership::NONE,
        )
        .unwrap();
        rewrite(&mut db, &m, "/f", 2);
        ops::set_retention(&mut db, &m, "/f", Some(0)).unwrap();
        ops::prune_content(&mut db, None).unwrap();
        let f = nid(&mut db, &m, "/f");
        assert_eq!(versions(&db, &f), [3]);
        assert_eq!(ops::read_all(&mut db, &m, "/f").unwrap(), b"BBBB");
    }

    #[test]
    fn the_volume_settings_validate() {
        let (mut db, vol, _) = rig();
        let nowhere = VolumeId("no-such-volume".into());
        assert!(matches!(
            ops::set_default_retention(&mut db, &vol, Some(0)),
            Err(FsError::Usage(_))
        ));
        assert!(matches!(
            ops::set_retention_on_delete(&mut db, &vol, -2),
            Err(FsError::Usage(_))
        ));
        assert!(matches!(
            ops::set_default_retention(&mut db, &nowhere, Some(1)),
            Err(FsError::NotFound { .. })
        ));
        assert!(matches!(
            ops::set_retention_on_delete(&mut db, &nowhere, 0),
            Err(FsError::NotFound { .. })
        ));
    }

    #[test]
    fn retention_on_delete_decides_what_a_deleted_entry_keeps() {
        // None is a volume nobody configured: the default is 1, a recycle bin
        let cases: [(Option<i64>, &[i64]); 4] = [
            (None, &[6]),
            (Some(1), &[6]),
            (Some(3), &[4, 5, 6]),
            (Some(-1), &[1, 2, 3, 4, 5, 6]),
        ];
        for (setting, kept) in cases {
            let (mut db, vol, m) = rig();
            if let Some(keep) = setting {
                ops::set_retention_on_delete(&mut db, &vol, keep).unwrap();
            }
            ops::create_entry(
                &mut db,
                &m,
                "/f",
                Some(b"0000"),
                crate::types::Ownership::NONE,
            )
            .unwrap();
            rewrite(&mut db, &m, "/f", 5);
            let f = nid(&mut db, &m, "/f");
            ops::remove(&mut db, &m, "/f").unwrap();
            // remove destroys nothing
            assert_eq!(versions(&db, &f), [1, 2, 3, 4, 5, 6]);
            ops::prune(&mut db, None).unwrap();
            ops::prune_content(&mut db, None).unwrap();
            assert_eq!(versions(&db, &f), kept, "retention_on_delete {setting:?}");
            assert!(node_exists(&db, &f), "still recoverable");
        }
    }

    #[test]
    fn retention_on_delete_zero_purges_deleted_nodes_at_prune() {
        let (mut db, vol, m) = rig();
        ops::set_retention_on_delete(&mut db, &vol, 0).unwrap();
        ops::create_entry(
            &mut db,
            &m,
            "/gone",
            Some(b"0000"),
            crate::types::Ownership::NONE,
        )
        .unwrap();
        ops::create_entry(
            &mut db,
            &m,
            "/stays",
            Some(b"1111"),
            crate::types::Ownership::NONE,
        )
        .unwrap();
        ops::create_container(&mut db, &m, "/d", crate::types::Ownership::NONE).unwrap();
        ops::create_container(&mut db, &m, "/d/sub", crate::types::Ownership::NONE).unwrap();
        ops::create_entry(
            &mut db,
            &m,
            "/d/sub/deep",
            Some(b"2222"),
            crate::types::Ownership::NONE,
        )
        .unwrap();
        ops::link(&mut db, &m, "/stays", "/alias").unwrap();
        let gone = nid(&mut db, &m, "/gone");
        let deep = nid(&mut db, &m, "/d/sub/deep");
        let d = nid(&mut db, &m, "/d");
        let stays = nid(&mut db, &m, "/stays");

        ops::remove(&mut db, &m, "/gone").unwrap();
        ops::remove_recursive(&mut db, &m, "/d").unwrap();
        // one of two placements: not a delete
        ops::remove(&mut db, &m, "/alias").unwrap();
        assert!(node_exists(&db, &gone), "remove alone destroys nothing");

        let report = ops::prune(&mut db, Some(&vol)).unwrap();
        assert_eq!(report.nodes_pruned, 4, "/gone, /d, /d/sub, /d/sub/deep");
        assert!(![&gone, &deep, &d].into_iter().any(|n| node_exists(&db, n)));
        assert!(node_exists(&db, &stays));
        assert_eq!(ops::read_all(&mut db, &m, "/stays").unwrap(), b"1111");
        assert_eq!(
            ops::prune_content(&mut db, Some(&vol))
                .unwrap()
                .chunks_pruned,
            2
        );
        assert!(ops::health_check(&mut db).unwrap().is_empty());
    }

    #[test]
    fn retention_on_delete_zero_spares_a_node_a_lock_holds() {
        let (mut db, vol, m) = rig();
        ops::set_retention_on_delete(&mut db, &vol, 0).unwrap();
        ops::create_entry(
            &mut db,
            &m,
            "/f",
            Some(b"0000"),
            crate::types::Ownership::NONE,
        )
        .unwrap();
        let f = nid(&mut db, &m, "/f");
        let lock = ops::lock(&mut db, &m, "/f", None).unwrap();
        // the holder may unlink its own open file
        ops::remove(&mut db, &m, "/f").unwrap();
        ops::prune(&mut db, Some(&vol)).unwrap();
        assert!(node_exists(&db, &f));
        ops::unlock(&mut db, &m, &lock.id).unwrap();
        ops::prune(&mut db, Some(&vol)).unwrap();
        assert!(!node_exists(&db, &f));
    }

    #[test]
    fn purge_destroys_now_whatever_the_volume_keeps() {
        let (mut db, vol, m) = rig();
        ops::set_retention_on_delete(&mut db, &vol, -1).unwrap();
        ops::create_entry(
            &mut db,
            &m,
            "/f",
            Some(b"0000"),
            crate::types::Ownership::NONE,
        )
        .unwrap();
        rewrite(&mut db, &m, "/f", 3);
        let f = nid(&mut db, &m, "/f");
        ops::purge(&mut db, &m, "/f").unwrap();
        assert!(!ops::exists(&mut db, &m, "/f").unwrap());
        assert!(!node_exists(&db, &f));
        assert!(versions(&db, &f).is_empty());
        // CV-7 reclaims the pool
        assert_eq!(
            ops::prune_content(&mut db, Some(&vol))
                .unwrap()
                .chunks_pruned,
            4
        );
        assert!(ops::health_check(&mut db).unwrap().is_empty());
    }

    #[test]
    fn purge_by_id_reaches_a_deleted_entry_and_refuses_roots_and_strangers() {
        let (mut db, vol, m) = rig();
        ops::set_retention_on_delete(&mut db, &vol, -1).unwrap();
        ops::create_entry(
            &mut db,
            &m,
            "/f",
            Some(b"0000"),
            crate::types::Ownership::NONE,
        )
        .unwrap();
        let f = nid(&mut db, &m, "/f");
        ops::remove(&mut db, &m, "/f").unwrap();
        ops::prune(&mut db, None).unwrap();
        assert!(node_exists(&db, &f), "no path leads here any more");
        ops::purge_by_id(&mut db, &m, &f).unwrap();
        assert!(!node_exists(&db, &f));
        assert!(matches!(
            ops::purge_by_id(&mut db, &m, &f),
            Err(FsError::NotFound { .. })
        ));

        // a placed node loses every placement
        ops::create_entry(
            &mut db,
            &m,
            "/g",
            Some(b"1111"),
            crate::types::Ownership::NONE,
        )
        .unwrap();
        ops::link(&mut db, &m, "/g", "/g2").unwrap();
        let g = nid(&mut db, &m, "/g");
        ops::purge_by_id(&mut db, &m, &g).unwrap();
        assert!(!ops::exists(&mut db, &m, "/g").unwrap());
        assert!(!ops::exists(&mut db, &m, "/g2").unwrap());

        let root = nid(&mut db, &m, "/");
        assert!(matches!(
            ops::purge_by_id(&mut db, &m, &root),
            Err(FsError::Unsupported { .. })
        ));
        let other = ops::create_volume(&mut db, Some("other"), 4, None, EncMode::None).unwrap();
        let om = ops::mount(&mut db, &other.id, &MountOptions::default()).unwrap();
        ops::create_entry(
            &mut db,
            &om,
            "/theirs",
            Some(b"x"),
            crate::types::Ownership::NONE,
        )
        .unwrap();
        let theirs = nid(&mut db, &om, "/theirs");
        assert!(matches!(
            ops::purge_by_id(&mut db, &m, &theirs),
            Err(FsError::VolumeMismatch)
        ));
    }
    // -- delta manifests (CV-4r1) -------------------------------------------

    /// The positions `version` itself wrote a manifest row for.
    fn rows(db: &Db, node: &NodeId, version: i64) -> Vec<i64> {
        let mut stmt = db
            .connection()
            .prepare(
                "SELECT chunk_index FROM content_version \
                 WHERE content_id = ?1 AND version = ?2 ORDER BY chunk_index",
            )
            .unwrap();
        stmt.query_map(rusqlite::params![&node.0, version], |r| r.get(0))
            .unwrap()
            .map(|v| v.unwrap())
            .collect()
    }

    fn entry(db: &mut Db, m: &MountId, path: &str, data: &[u8]) -> NodeId {
        ops::create_entry(db, m, path, Some(data), crate::types::Ownership::NONE).unwrap()
    }

    fn clean(db: &mut Db, m: &MountId) {
        let report = ops::verify(db, m, false).unwrap();
        assert!(report.problems.is_empty(), "{:?}", report.problems);
    }

    #[test]
    fn a_version_writes_rows_only_for_the_positions_it_changes() {
        let (mut db, _, m) = rig();
        let f = entry(&mut db, &m, "/f", b"aaaabbbbccccdd");
        assert_eq!(rows(&db, &f, 1), [0, 1, 2, 3]);
        ops::append(&mut db, &m, "/f", b"EEEE").unwrap();
        assert_eq!(rows(&db, &f, 2), [3, 4]);
        ops::truncate(&mut db, &m, "/f", 8).unwrap(); // aligned: no row at all
        assert_eq!(rows(&db, &f, 3), [0i64; 0]);
        ops::truncate(&mut db, &m, "/f", 6).unwrap(); // the boundary chunk
        assert_eq!(rows(&db, &f, 4), [1]);
        ops::write_range(&mut db, &m, "/f", 1, b"Z").unwrap();
        assert_eq!(rows(&db, &f, 5), [0]);
        let mut w =
            ops::open_write(&mut db, &m, "/f", crate::types::WriteMode::Append, None).unwrap();
        w.write(&mut db, b"XXXXXXYY").unwrap();
        w.close(&mut db).unwrap();
        assert_eq!(rows(&db, &f, 6), [1, 2, 3]);
        assert_eq!(ops::read_all(&mut db, &m, "/f").unwrap(), b"aZaabbXXXXXXYY");
        clean(&mut db, &m);
    }

    #[test]
    fn prune_keeps_the_rows_the_floor_still_reads() {
        let (mut db, _, m) = rig();
        let f = entry(&mut db, &m, "/f", b"aaaabbbbccccdddd"); // v1
        ops::write_range(&mut db, &m, "/f", 4, b"BBBB").unwrap(); // v2: position 1
        ops::write_range(&mut db, &m, "/f", 4, b"bbBB").unwrap(); // v3: position 1
        ops::truncate(&mut db, &m, "/f", 12).unwrap(); // v4: no rows
        ops::set_retention(&mut db, &m, "/f", Some(2)).unwrap(); // keeps v4 and v3

        // v1's and v2's rows at position 1 are shadowed by v3's; the rest of
        // v1 is still read by v3, the floor, which is four chunks long
        assert_eq!(
            ops::prune_content(&mut db, None).unwrap().versions_pruned,
            2
        );
        assert_eq!(versions(&db, &f), [3, 4]);
        assert_eq!(rows(&db, &f, 1), [0, 2, 3]);
        assert_eq!(rows(&db, &f, 2), [0i64; 0]);
        assert_eq!(
            ops::prune_content(&mut db, None).unwrap().versions_pruned,
            0
        );

        // the floor moves up to v4: position 3 is past its end
        ops::set_retention(&mut db, &m, "/f", Some(1)).unwrap();
        assert_eq!(
            ops::prune_content(&mut db, None).unwrap().versions_pruned,
            1
        );
        assert_eq!(versions(&db, &f), [4]);
        assert_eq!(rows(&db, &f, 1), [0, 2]);
        assert_eq!(rows(&db, &f, 3), [1]);
        assert_eq!(ops::read_all(&mut db, &m, "/f").unwrap(), b"aaaabbBBcccc");
        clean(&mut db, &m);
    }

    #[test]
    fn an_abandoned_stream_is_staged_out_of_reach_and_pruned() {
        let (mut db, _, m) = rig();
        let f = entry(&mut db, &m, "/f", b"aaaabbbbcccc");
        let mut w =
            ops::open_write(&mut db, &m, "/f", crate::types::WriteMode::Truncate, None).unwrap();
        w.write(&mut db, b"XXXXYYYYZZZZ").unwrap(); // full chunks staged
        let staged: i64 = db
            .connection()
            .query_row(
                "SELECT min(version) FROM content_version WHERE content_id = ?1 AND version > 1",
                [&f.0],
                |r| r.get(0),
            )
            .unwrap();
        assert!(staged > 1 << 62);
        w.abort(&mut db).unwrap();
        ops::write_range(&mut db, &m, "/f", 0, b"N").unwrap(); // v2: position 0 only
        assert_eq!(ops::read_all(&mut db, &m, "/f").unwrap(), b"Naaabbbbcccc");
        ops::prune_content(&mut db, None).unwrap();
        assert_eq!(rows(&db, &f, staged), [0i64; 0]);
        assert_eq!(ops::read_all(&mut db, &m, "/f").unwrap(), b"Naaabbbbcccc");
    }

    #[test]
    fn an_append_stream_pins_its_prefix_when_the_base_moves() {
        let (mut db, _, m) = rig();
        entry(&mut db, &m, "/f", b"aaaabbbbcc");
        let mut w =
            ops::open_write(&mut db, &m, "/f", crate::types::WriteMode::Append, None).unwrap();
        w.write(&mut db, b"DDEEEE").unwrap();
        ops::write_all(&mut db, &m, "/f", &[b'z'; 20]).unwrap(); // same mount, mid-stream
        w.close(&mut db).unwrap();
        assert_eq!(
            ops::read_all(&mut db, &m, "/f").unwrap(),
            b"aaaabbbbccDDEEEE"
        );
        clean(&mut db, &m);
    }

    #[test]
    fn random_writes_and_prunes_match_a_plain_buffer() {
        let (mut db, _, m) = rig();
        let mut seed = 11u64;
        let mut next = |bound: u64| {
            seed = seed
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            (seed >> 33) % bound
        };
        let mut reference = b"seed".to_vec();
        entry(&mut db, &m, "/f", &reference);
        for step in 0..400 {
            let op = next(6);
            let len = next(10) as usize + 1;
            let data: Vec<u8> = (0..len).map(|_| next(255) as u8 + 1).collect();
            match op {
                0..=2 => {
                    let off = next(reference.len() as u64 + 6) as usize;
                    if off + len > reference.len() {
                        reference.resize(off + len, 0);
                    }
                    reference[off..off + len].copy_from_slice(&data);
                    ops::write_range(&mut db, &m, "/f", off as u64, &data).unwrap();
                }
                3 => {
                    reference.extend_from_slice(&data);
                    ops::append(&mut db, &m, "/f", &data).unwrap();
                }
                4 => {
                    let size = next(reference.len() as u64 + 9) as usize;
                    reference.resize(size, 0);
                    ops::truncate(&mut db, &m, "/f", size as u64).unwrap();
                }
                _ => {
                    let append = next(2) == 0;
                    let mode = if append {
                        crate::types::WriteMode::Append
                    } else {
                        reference.clear();
                        crate::types::WriteMode::Truncate
                    };
                    let mut w = ops::open_write(&mut db, &m, "/f", mode, None).unwrap();
                    w.write(&mut db, &data).unwrap();
                    w.close(&mut db).unwrap();
                    reference.extend_from_slice(&data);
                }
            }
            assert_eq!(
                ops::read_all(&mut db, &m, "/f").unwrap(),
                reference,
                "step {step}"
            );
            if step % 7 == 0 {
                let keep = [None, Some(1), Some(1), Some(2), Some(3)][next(5) as usize];
                ops::set_retention(&mut db, &m, "/f", keep).unwrap();
                ops::prune_content(&mut db, None).unwrap();
                assert_eq!(
                    ops::read_all(&mut db, &m, "/f").unwrap(),
                    reference,
                    "prune {step}"
                );
                clean(&mut db, &m);
            }
        }
    }

    #[test]
    fn an_era4_file_gains_commit_records_and_reads_as_before() {
        let (mut db, vol, m) = rig();
        let f = entry(&mut db, &m, "/f", b"0000aa");
        ops::write_all(&mut db, &m, "/f", b"1111bbbbc").unwrap();
        let cut = entry(&mut db, &m, "/cut", b"xxxx");
        ops::truncate(&mut db, &m, "/cut", 0).unwrap(); // committed at 2, no chunks
        // an era-4 writer that died mid-stream left rows at committed + 1
        db.connection()
            .execute(
                "INSERT INTO content_version (content_id, version, chunk_index, chunk_hash) \
                 SELECT content_id, 3, chunk_index, chunk_hash FROM content_version \
                 WHERE content_id = ?1 AND version = 1",
                [&f.0],
            )
            .unwrap();
        let commits = |db: &Db, node: &NodeId| -> Vec<(i64, i64, i64)> {
            let mut stmt = db
                .connection()
                .prepare(
                    "SELECT version, size, chunks FROM content_commit \
                     WHERE content_id = ?1 ORDER BY version",
                )
                .unwrap();
            stmt.query_map([&node.0], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
                .unwrap()
                .map(|v| v.unwrap())
                .collect()
        };
        for _ in 0..2 {
            // the second pass is the crash-rerun: stamp wound back
            db.connection()
                .execute_batch(
                    "DROP TABLE content_commit; DROP INDEX content_version_position; \
                     PRAGMA user_version = 4;",
                )
                .unwrap();
            crate::db::install_schema(db.connection()).unwrap();
            assert_eq!(commits(&db, &f), [(1, 6, 2), (2, 9, 3)]);
            assert_eq!(commits(&db, &cut), [(1, 4, 1), (2, 0, 0)]);
            assert_eq!(rows(&db, &f, 3), [0i64; 0]);
            assert_eq!(ops::read_all(&mut db, &m, "/f").unwrap(), b"1111bbbbc");
            assert_eq!(ops::read_all(&mut db, &m, "/cut").unwrap(), b"");
            clean(&mut db, &m);
        }
        ops::write_range(&mut db, &m, "/f", 5, b"Z").unwrap(); // one row, not three
        assert_eq!(rows(&db, &f, 3), [1]);
        ops::set_default_retention(&mut db, &vol, Some(1)).unwrap();
        ops::prune_content(&mut db, None).unwrap();
        assert_eq!(ops::read_all(&mut db, &m, "/f").unwrap(), b"1111bZbbc");
        clean(&mut db, &m);
    }
}
