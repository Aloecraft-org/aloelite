//! Session / lifecycle: volumes and mounts.

use rusqlite::named_params;
use zeroize::Zeroizing;

use crate::crypto::{self, Cipher, EncMode};
use crate::db::{Db, Session};
use crate::errors::{FsError, Result};
use crate::records::{MountInfo, VolumeInfo};
use crate::resolve::resolve;
use crate::templates::mutation::{
    CREATE_MOUNT, CREATE_NODE, CREATE_VOLUME, LINK_ROOT, RENEW_MOUNT, SET_MOUNT_STATE,
    UPDATE_VOLUME_CRYPTO,
};
use crate::templates::resolution::{
    GET_MOUNT, GET_VOLUME, GET_VOLUME_CRYPTO, LIST_MOUNTS, LIST_VOLUMES,
};
use crate::templates::validation::CHECK_RW_OVERLAP;
use crate::types::{Access, MountId, MountState, NodeId, NodeType, VolumeId};

use super::{abs_path, require_mount};

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// The optional parameters of `mount` (mount-api.yaml `session.mount`).
#[derive(Debug, Clone)]
pub struct MountOptions<'a> {
    /// Anchor, relative to the volume root. Defaults to the root.
    pub at: &'a str,
    /// Lease per ACC-3; `None` never expires.
    pub ttl_ms: Option<i64>,
    /// Required for an encrypted volume, refused for a plain one (ENC-3).
    pub pin: Option<&'a [u8]>,
    /// D-4 access mode.
    pub access: Access,
    /// Optional tenant/user identity for policy.
    pub principal: Option<&'a str>,
    /// D-4: stack an rw mount over a subtree another rw mount already covers.
    pub allow_overlap: bool,
}

impl Default for MountOptions<'_> {
    fn default() -> Self {
        MountOptions {
            at: "/",
            ttl_ms: None,
            pin: None,
            access: Access::Rw,
            principal: None,
            allow_overlap: false,
        }
    }
}

/// Create a volume and its root container, linked, in one transaction.
///
/// `chunk_size` (CV-1) is fixed here and immutable thereafter. With a `pin`
/// the volume is encrypted: a random volume key `K_v` is sealed under
/// `K_u = Argon2id(pin, H_v)`, `H_v = SHA256(volume_id || root_node_id)`;
/// the wrapped key and nonce land on the volume row and the PIN itself is
/// never stored. `enc_mode` then selects convergent (dedup preserved) or
/// random (dedup sacrificed for zero equality leakage); without a pin the
/// volume is `none` whatever `enc_mode` says.
pub fn create_volume(
    db: &mut Db,
    name: Option<&str>,
    chunk_size: usize,
    pin: Option<&[u8]>,
    enc_mode: EncMode,
) -> Result<VolumeInfo> {
    let mode = if pin.is_some() {
        enc_mode
    } else {
        EncMode::None
    };
    let vid = db.txn(|db| {
        let vid = VolumeId(db.gen_id());
        let created_at = db.now_ns();
        db.run(
            CREATE_VOLUME,
            named_params! {
                ":id": vid,
                ":name": name,
                ":created_at": created_at,
                ":chunk_size": chunk_size as i64,
                ":enc_mode": mode.as_str(),
            },
        )?;
        // the root container is created with the volume so monotonic ids work
        let root_created = db.now_ns();
        let root = NodeId(db.create_monotonic(
            CREATE_NODE,
            Some(&vid),
            named_params! {
                ":type": NodeType::Container,
                ":name": "/",
                ":created_at": root_created,
                ":modified_at": Option::<i64>::None,
                ":volume": vid,
                ":metadata": Option::<String>::None,
            },
        )?);
        db.run(LINK_ROOT, named_params! { ":volume": vid, ":root": root })?;
        if let Some(pin) = pin {
            // H_v needs the root id, so seal after link_root, still in this txn.
            let h_v = crypto::volume_hash(&vid.0, &root.0);
            let k_u = crypto::derive_unlock_key(pin, &h_v);
            let k_v = crypto::new_volume_key(db.rng());
            let (wrapped, wrap_nonce) = crypto::wrap_volume_key(&k_u, &k_v, db.rng());
            db.run(
                UPDATE_VOLUME_CRYPTO,
                named_params! {
                    ":volume": vid,
                    ":enc_mode": mode.as_str(),
                    ":wrapped_key": wrapped,
                    ":wrap_nonce": wrap_nonce.as_slice(),
                },
            )?;
        }
        Ok(vid)
    })?;
    db.one(
        GET_VOLUME,
        named_params! { ":volume": vid },
        VolumeInfo::from_row,
    )?
    .ok_or_else(|| FsError::internal("volume vanished after create"))
}

/// Rotate an encrypted volume's PIN: unwrap `K_v` with the old `K_u`, re-wrap
/// under the new one (fresh nonce). The volume key — and every chunk, address
/// and dedup relationship — is unchanged, and live mounts are unaffected
/// (they hold `K_v`, not `K_u`).
pub fn change_pin(db: &mut Db, volume: &VolumeId, old_pin: &[u8], new_pin: &[u8]) -> Result<()> {
    db.txn(|db| {
        let crow = volume_crypto(db, volume)?;
        if crow.enc_mode == EncMode::None {
            return Err(FsError::EncryptionRequired {
                msg: "volume is not encrypted; no PIN to change".into(),
            });
        }
        let h_v = crypto::volume_hash(&volume.0, &crow.root.0);
        let k_u_old = crypto::derive_unlock_key(old_pin, &h_v);
        let k_v = crypto::unwrap_volume_key(&k_u_old, &crow.wrapped_key, &crow.wrap_nonce)
            .map_err(|_| FsError::BadKey)?;
        let k_u_new = crypto::derive_unlock_key(new_pin, &h_v);
        let (wrapped, wrap_nonce) = crypto::wrap_volume_key(&k_u_new, &k_v, db.rng());
        db.run(
            UPDATE_VOLUME_CRYPTO,
            named_params! {
                ":volume": volume,
                ":enc_mode": crow.enc_mode.as_str(),
                ":wrapped_key": wrapped,
                ":wrap_nonce": wrap_nonce.as_slice(),
            },
        )?;
        Ok(())
    })
}

pub fn list_volumes(db: &mut Db) -> Result<Vec<VolumeInfo>> {
    db.all(LIST_VOLUMES, &[], VolumeInfo::from_row)
}

/// Open a mount on a volume, anchored at `opts.at`. Returns the durable id.
///
/// For an encrypted volume the PIN derives `K_u`, unwraps `K_v`, installs
/// the chunk cipher on this connection for the life of the mount, and mints a
/// per-mount token plus a memory-only sealed mount secret (so a re-attach
/// within the process needs only the token and `N_m`, not the PIN); see
/// [`Db::session`]. A wrong PIN is `bad_key`; a PIN on a plain volume, or
/// none on an encrypted one, is `encryption_required`.
pub fn mount(db: &mut Db, volume: &VolumeId, opts: &MountOptions<'_>) -> Result<MountId> {
    db.txn(|db| {
        let crow = volume_crypto(db, volume)?;
        let anchor = resolve(db, &crow.root, opts.at)?.node; // mount point within the volume
        // D-4 admission policy: at most one rw mount per subtree by default.
        // Authorization, not correctness -- ids are safe under arbitrary
        // overlap (D-1/D-2) and entry write locks arbitrate write conflicts --
        // so overlap is a deliberate opt-in, not a workaround.
        if opts.access == Access::Rw && !opts.allow_overlap {
            let clash: Option<MountId> = db.scalar(
                CHECK_RW_OVERLAP,
                named_params! { ":volume": volume, ":anchor": anchor },
            )?;
            if let Some(clash) = clash {
                return Err(FsError::MountConflict {
                    conflicting_mount: clash.0,
                });
            }
        }
        let now = db.now_ns();
        let expires = opts.ttl_ms.map(|ttl| now + ttl * 1_000_000);
        let mid = MountId(db.gen_id());
        let n_m = crypto::new_mount_nonce(db.rng());
        let created_at = db.now_ns();
        db.run(
            CREATE_MOUNT,
            named_params! {
                ":id": mid,
                ":volume": volume,
                ":mount_point": anchor,
                ":expires_at": expires,
                ":created_at": created_at,
                ":n_m": n_m.as_slice(),
                ":access": opts.access,
                ":principal": opts.principal,
            },
        )?;
        match crow.enc_mode {
            EncMode::None => {
                if opts.pin.is_some() {
                    return Err(FsError::EncryptionRequired {
                        msg: "volume is not encrypted but a pin was given".into(),
                    });
                }
                db.cipher = Cipher::identity();
                db.session = None;
            }
            mode => {
                let Some(pin) = opts.pin else {
                    return Err(FsError::EncryptionRequired {
                        msg: "volume is encrypted; a pin is required".into(),
                    });
                };
                let h_v = crypto::volume_hash(&volume.0, &crow.root.0);
                let k_u = crypto::derive_unlock_key(pin, &h_v);
                let k_v = crypto::unwrap_volume_key(&k_u, &crow.wrapped_key, &crow.wrap_nonce)
                    .map_err(|_| FsError::BadKey)?;
                db.cipher = Cipher::for_volume(&k_v, &volume.0, mode);
                let token = crypto::new_token(db.rng());
                let (mount_secret, session_nonce) =
                    crypto::seal_mount_secret(&token, &n_m, &k_v, db.rng());
                db.session = Some(Session {
                    mount_id: mid.clone(),
                    volume: volume.clone(),
                    enc_mode: mode,
                    token: Zeroizing::new(token),
                    mount_nonce: n_m,
                    mount_secret: Zeroizing::new(mount_secret),
                    session_nonce,
                });
            }
        }
        Ok(mid)
    })
}

/// Mark the mount invalid. Lock reclamation is deferred to `prune` (ACC-10).
/// Tears down the session cipher: `K_v` and the sealed mount secret leave
/// memory, so later operations fall back to the identity cipher.
pub fn unmount(db: &mut Db, mount: &MountId) -> Result<()> {
    db.txn(|db| {
        db.run(
            SET_MOUNT_STATE,
            named_params! { ":mount": mount, ":state": MountState::Unmounted },
        )?;
        Ok(())
    })?;
    if db.session.as_ref().is_some_and(|s| &s.mount_id == mount) {
        db.session = None;
        db.cipher = Cipher::identity();
    }
    Ok(())
}

/// Heartbeat: extend (or clear) the lease on a live mount.
pub fn renew_mount(db: &mut Db, mount: &MountId, ttl_ms: Option<i64>) -> Result<MountInfo> {
    db.txn(|db| {
        require_mount(db, mount, false)?;
        let now = db.now_ns();
        let expires = ttl_ms.map(|ttl| now + ttl * 1_000_000);
        db.run(
            RENEW_MOUNT,
            named_params! { ":mount": mount, ":expires_at": expires },
        )?;
        Ok(())
    })?;
    mount_info(db, mount)
}

pub fn mount_info(db: &mut Db, mount: &MountId) -> Result<MountInfo> {
    let (mount_point, row) = db
        .one(GET_MOUNT, named_params! { ":mount": mount }, |r| {
            Ok((
                r.get::<_, NodeId>("mount_point")?,
                MountInfo::from_row(r, None)?,
            ))
        })?
        .ok_or_else(|| FsError::not_found(format!("mount {mount} does not exist")))?;
    let mount_path = abs_path(db, &mount_point)?;
    Ok(MountInfo {
        mount_path: Some(mount_path),
        ..row
    })
}

/// List durable mounts (ACC-1a), optionally scoped to one volume. Reads the
/// raw mount table, so expired mounts appear; retired rows only with
/// `include_unmounted`. A mount whose anchor no longer resolves (ACC-5) is
/// returned with `mount_path: None` rather than aborting the listing.
pub fn list_mounts(
    db: &mut Db,
    volume: Option<&VolumeId>,
    include_unmounted: bool,
) -> Result<Vec<MountInfo>> {
    let rows = db.all(
        LIST_MOUNTS,
        named_params! { ":volume": volume, ":include_unmounted": i64::from(include_unmounted) },
        |r| MountInfo::from_row(r, None),
    )?;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        let mount_path = match abs_path(db, &row.mount_point) {
            Ok(p) => Some(p),
            Err(FsError::NotFound { .. } | FsError::Corrupt { .. }) => None,
            Err(e) => return Err(e),
        };
        out.push(MountInfo { mount_path, ..row });
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// depth
// ---------------------------------------------------------------------------

struct VolumeCrypto {
    root: NodeId,
    enc_mode: EncMode,
    wrapped_key: Vec<u8>,
    wrap_nonce: Vec<u8>,
}

/// The volume's crypto row, or `not_found` for a missing volume or one with
/// no root yet.
fn volume_crypto(db: &mut Db, volume: &VolumeId) -> Result<VolumeCrypto> {
    let row = db.one(
        GET_VOLUME_CRYPTO,
        named_params! { ":volume": volume },
        |r| {
            let root: Option<NodeId> = r.get("root_node_id")?;
            let mode: String = r.get("enc_mode")?;
            let enc_mode = EncMode::parse(&mode).ok_or_else(|| {
                FsError::corrupt(format!("volume {volume} has enc_mode {mode:?}"))
            })?;
            Ok((
                root,
                enc_mode,
                r.get::<_, Option<Vec<u8>>>("wrapped_key")?,
                r.get::<_, Option<Vec<u8>>>("wrap_nonce")?,
            ))
        },
    )?;
    match row {
        Some((Some(root), enc_mode, wrapped_key, wrap_nonce)) => Ok(VolumeCrypto {
            root,
            enc_mode,
            wrapped_key: wrapped_key.unwrap_or_default(),
            wrap_nonce: wrap_nonce.unwrap_or_default(),
        }),
        _ => Err(FsError::not_found(format!(
            "volume {volume} or its root not found"
        ))),
    }
}
