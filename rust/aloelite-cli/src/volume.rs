//! Volume selection and the two facts about a volume the listings need.
//!
//! `-v` is name-first, id-fallback: a name that matches wins; otherwise the
//! reference accepts an id as canonical uuid7 or as bare 32-hex (dashes
//! stripped, lower-cased, re-dashed). With no `-v`, a file holding exactly
//! one volume uses it, and several is a refusal to guess that lists them.

use aloelite_core::ops;
use aloelite_core::templates::resolution::GET_VOLUME_CRYPTO;
use aloelite_core::types::VolumeId;
use aloelite_core::{Db, FsError};

use crate::fail::{Fail, Result, fail};

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// A volume id as canonical uuid7 or bare hex → canonical lowercase;
/// anything else untouched (it may be a name).
pub fn normalize_ref(r: &str) -> String {
    let bare: String = r.chars().filter(|c| *c != '-').collect();
    if bare.len() == 32 && bare.chars().all(|c| c.is_ascii_hexdigit()) {
        let b = bare.to_ascii_lowercase();
        return format!(
            "{}-{}-{}-{}-{}",
            &b[0..8],
            &b[8..12],
            &b[12..16],
            &b[16..20],
            &b[20..32]
        );
    }
    r.to_owned()
}

/// The facade's rule for a name: the most recently created volume of that
/// name wins (`created_at`, then id), so the answer is stable whatever the
/// row order. The same rule as the browser surface's `resolve_volume_name`.
pub fn resolve_volume_name(
    db: &mut Db,
    name: &str,
) -> std::result::Result<Option<VolumeId>, FsError> {
    Ok(ops::list_volumes(db)?
        .into_iter()
        .filter(|v| v.name.as_deref() == Some(name))
        .max_by(|x, y| (x.created_at, &x.id.0).cmp(&(y.created_at, &y.id.0)))
        .map(|v| v.id))
}

/// The volume `-v` names, or the file's only one.
pub fn select_volume(db: &mut Db, r: Option<&str>) -> Result<VolumeId> {
    let vols = ops::list_volumes(db)?;
    let Some(r) = r else {
        return match vols.len() {
            1 => Ok(vols.into_iter().next().unwrap().id),
            0 => fail("file contains no volumes"),
            _ => {
                let names: Vec<String> = vols
                    .iter()
                    .map(|v| {
                        format!(
                            "{} ({}…)",
                            v.name.as_deref().unwrap_or("(unnamed)"),
                            &v.id.0[..8]
                        )
                    })
                    .collect();
                fail(format!(
                    "multiple volumes; pick one with -v: {}",
                    names.join(", ")
                ))
            }
        };
    };
    let r = normalize_ref(r);
    if let Some(id) = resolve_volume_name(db, &r)? {
        return Ok(id);
    }
    if let Some(v) = vols.into_iter().find(|v| v.id.0 == r) {
        return Ok(v.id);
    }
    fail(format!("no volume named or identified by {r:?}"))
}

/// Whether the volume is encrypted — read from its row, as the reference
/// does: the Mount API has no query for it short of attempting a mount.
pub fn is_encrypted(db: &Db, volume: &VolumeId) -> Result<bool> {
    let mode: Option<String> = db
        .connection()
        .query_row(GET_VOLUME_CRYPTO, &[(":volume", &volume.0)], |row| {
            row.get("enc_mode")
        })
        .map(Some)
        .or_else(|e| match e {
            rusqlite::Error::QueryReturnedNoRows => Ok(None),
            other => Err(other),
        })
        .map_err(|e| Fail::Engine(FsError::Sqlite(e)))?;
    Ok(mode.is_some_and(|m| m != "none"))
}

/// `encrypted` / `plain`, the listing's label.
pub fn enc_label(db: &Db, volume: &VolumeId) -> &'static str {
    match is_encrypted(db, volume) {
        Ok(true) => "encrypted",
        _ => "plain",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ids_normalize_and_names_pass_through() {
        let dashed = "0192b3c4-d5e6-7f80-91a2-b3c4d5e6f708";
        assert_eq!(normalize_ref(dashed), dashed);
        assert_eq!(
            normalize_ref(&dashed.replace('-', "").to_uppercase()),
            dashed
        );
        assert_eq!(normalize_ref("vault"), "vault");
        assert_eq!(normalize_ref("deadbeef"), "deadbeef"); // hex but not 32 wide
    }
}
