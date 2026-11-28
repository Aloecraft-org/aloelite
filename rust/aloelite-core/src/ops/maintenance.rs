//! Maintenance: prune, prune_content, verify, health_check.

use std::collections::{HashMap, HashSet};

use rusqlite::named_params;

use crate::content::chunk_hash;
use crate::db::Db;
use crate::errors::Result;
use crate::records::{Anomaly, ContentPruneReport, PruneReport, VerifyReport};
use crate::templates::maintenance::{
    HEALTH_CHECK, PRUNE_CONTENT_CHUNKS, PRUNE_CONTENT_VERSIONS, PRUNE_DELETED_EDGES, PRUNE_LOCKS,
    PRUNE_NODES,
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
        chunk_index: i64,
        chunk_hash: String,
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
        let refs = &groups[key];
        let (node, version) = key;
        let idxs: Vec<i64> = refs.iter().map(|r| r.chunk_index).collect();
        if idxs.iter().copied().ne(0..idxs.len() as i64) {
            problems.push(format!("gap node={node} v={version} indexes={idxs:?}"));
        }
        let mut total = 0i64;
        for r in refs {
            if !r.present {
                problems.push(format!("missing-chunk node={node} hash={}", r.chunk_hash));
                continue;
            }
            total += r.chunk_length.unwrap_or(0);
            if seen_set.insert(r.chunk_hash.clone()) {
                seen.push(r.chunk_hash.clone());
            }
        }
        if total != refs[0].size {
            problems.push(format!(
                "size-mismatch node={node} manifest={} chunks={total}",
                refs[0].size
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
            .prepare(
                "SELECT DISTINCT version FROM content_version WHERE content_id = ?1 \
                 ORDER BY version",
            )
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
}
