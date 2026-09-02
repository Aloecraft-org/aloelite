//! Maintenance: prune, prune_content, verify, health_check.

use std::collections::{HashMap, HashSet};

use rusqlite::named_params;

use crate::content::chunk_hash;
use crate::db::Db;
use crate::errors::Result;
use crate::records::{Anomaly, ContentPruneReport, PruneReport, VerifyReport};
use crate::templates::maintenance::{
    HEALTH_CHECK, PRUNE_CONTENT_CHUNKS, PRUNE_CONTENT_VERSIONS, PRUNE_LOCKS, PRUNE_NODES,
};
use crate::templates::resolution::{GET_CHUNK, VERIFY_MANIFEST};
use crate::types::{MountId, VolumeId};

use super::require_mount;

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// Lazy sweep: volatile nodes (PI-3) and invalid locks (ACC-10).
pub fn prune(db: &mut Db, volume: Option<&VolumeId>) -> Result<PruneReport> {
    db.txn(|db| {
        let locks = db.run(PRUNE_LOCKS, &[])?;
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
