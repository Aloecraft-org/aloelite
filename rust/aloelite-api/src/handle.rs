//! One engine handle, and the dispatch that drives it by operation name.
//!
//! [`Handle`] is the state a frontend wraps: the engine, plus the streaming
//! descriptors opened on it. [`Handle::call`] is the whole API — an
//! operation name from [`crate::table::OPS`], arguments keyed by that
//! operation's parameter names, and a result built through the
//! [`Surface`] the caller names.
//!
//! The dispatch below is the only copy of the mapping from the spec's
//! operation names to `aloelite_core::ops`. A frontend supplies a value
//! type and gets every operation; it cannot supply half of them, and two
//! frontends cannot disagree about what `create_volume` takes.

use std::collections::HashMap;

use aloelite_core::ops::{self, MountOptions};
use aloelite_core::types::{FdId, NodeId, VolumeId};
use aloelite_core::{Db, Descriptor, FsError};
use serde::Serialize;

use crate::args::Args;
use crate::table::{DEFAULT_CHUNK_SIZE, EXTRA_OPS, OPS};
use crate::value::Surface;

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// The engine behind one handle, with the descriptors open on it.
///
/// Closing is one-way and idempotent: [`Handle::shut`] aborts whatever is
/// still open, closes the engine, and every later call is a `usage` error.
pub struct Handle {
    db: Option<Db>,
    fds: HashMap<String, Descriptor>,
}

impl Handle {
    /// Wrap an engine someone else opened. Which is the only way to build
    /// one: how a connection is provisioned is `aloelite-store`'s question,
    /// and it has a different answer on every target.
    pub fn new(db: Db) -> Handle {
        Handle {
            db: Some(db),
            fds: HashMap::new(),
        }
    }

    /// Run one operation: `op` is a name from [`OPS`] (or [`EXTRA_OPS`]),
    /// `args` the arguments keyed by its parameter names, or nothing.
    pub fn call<S: Surface>(&mut self, op: &str, args: &S::In) -> Result<S::Out, FsError> {
        let spec = OPS
            .iter()
            .chain(EXTRA_OPS)
            .find(|o| o.name == op)
            .ok_or_else(|| FsError::usage(format!("no operation {op:?}")))?;
        let a = Args::read(op, args)?;
        a.allow(spec.args)?;
        let Handle { db, fds } = self;
        let db = db
            .as_mut()
            .ok_or_else(|| FsError::usage(format!("{op}: this handle is closed")))?;
        if spec.args.first() == Some(&"fd") {
            descriptor_op::<S>(op, &a, fds, db)
        } else {
            engine_op::<S>(op, &a, db, fds)
        }
    }

    /// Abort open descriptors and close the engine. Idempotent; a closed
    /// handle answers `Ok`.
    pub fn shut(&mut self) -> Result<(), FsError> {
        let Some(mut db) = self.db.take() else {
            return Ok(());
        };
        for (_, mut d) in self.fds.drain() {
            let _ = d.abort(&mut db);
        }
        db.close()
    }

    /// Whether [`Handle::shut`] has run.
    pub fn is_closed(&self) -> bool {
        self.db.is_none()
    }

    /// How many streaming descriptors are open.
    pub fn open_descriptors(&self) -> usize {
        self.fds.len()
    }

    /// Every name [`Handle::call`] accepts, for a host building its own
    /// wrapper.
    pub fn operations() -> Vec<String> {
        OPS.iter()
            .chain(EXTRA_OPS)
            .map(|o| o.name.to_owned())
            .collect()
    }
}

impl std::fmt::Debug for Handle {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Handle")
            .field("closed", &self.is_closed())
            .field("open_descriptors", &self.fds.len())
            .finish()
    }
}

// ---------------------------------------------------------------------------
// depth: dispatch
// ---------------------------------------------------------------------------

/// The spec's `Descriptor` record: the streaming handle's projection.
#[derive(Serialize)]
struct DescriptorRecord<'a> {
    fd: &'a FdId,
    node: &'a NodeId,
    writable: bool,
}

fn register<S: Surface>(fds: &mut HashMap<String, Descriptor>, d: Descriptor) -> S::Out {
    let projected = S::record(&DescriptorRecord {
        fd: &d.fd,
        node: &d.node,
        writable: d.writable,
    });
    fds.insert(d.fd.0.clone(), d);
    projected
}

fn descriptor_op<S: Surface>(
    op: &str,
    a: &Args<S::In>,
    fds: &mut HashMap<String, Descriptor>,
    db: &mut Db,
) -> Result<S::Out, FsError> {
    let fd = a.str("fd")?;
    let d = fds
        .get_mut(&fd)
        .ok_or_else(|| FsError::usage(format!("{op}: no open descriptor {fd:?}")))?;
    Ok(match op {
        "read" => {
            // a negative len reads to the end, as the reference's read(-1)
            let n = a.opt_int("len")?.filter(|n| *n >= 0).map(|n| n as usize);
            S::bytes(&d.read(db, n)?)
        }
        "write" => S::record(&d.write(db, &a.bytes("data")?)?),
        "seek" => S::record(&d.seek(db, a.int("offset")?, a.whence("whence")?)?),
        "tell" => S::record(&d.tell()?),
        "close" | "abort" => {
            let result = if op == "close" {
                d.close(db)
            } else {
                d.abort(db)
            };
            fds.remove(&fd);
            result?;
            S::unit()
        }
        other => {
            return Err(FsError::usage(format!(
                "{other} is not a descriptor operation"
            )));
        }
    })
}

fn engine_op<S: Surface>(
    op: &str,
    a: &Args<S::In>,
    db: &mut Db,
    fds: &mut HashMap<String, Descriptor>,
) -> Result<S::Out, FsError> {
    let mnt = || a.mount();
    let path = || a.str("path");
    Ok(match op {
        // -- session -------------------------------------------------------
        "create_volume" => {
            let name = a.opt_str("name")?;
            let pin = a.opt_bytes("pin")?;
            let chunk_size = match a.opt_int("chunk_size")? {
                None => DEFAULT_CHUNK_SIZE,
                Some(n) => usize::try_from(n)
                    .ok()
                    .filter(|n| *n > 0)
                    .ok_or_else(|| FsError::usage("create_volume: chunk_size must be positive"))?,
            };
            S::record(&ops::create_volume(
                db,
                name.as_deref(),
                chunk_size,
                pin.as_deref(),
                a.enc_mode("enc_mode")?,
            )?)
        }
        "change_pin" => {
            ops::change_pin(
                db,
                &a.volume("volume")?,
                &a.bytes("old_pin")?,
                &a.bytes("new_pin")?,
            )?;
            S::unit()
        }
        "list_volumes" => S::record(&ops::list_volumes(db)?),
        "resolve_volume_name" => S::record(&resolve_volume_name(db, &a.str("name")?)?),
        "mount" => {
            let at = a.opt_str("at")?;
            let pin = a.opt_bytes("pin")?;
            let principal = a.opt_str("principal")?;
            let opts = MountOptions {
                at: at.as_deref().unwrap_or("/"),
                ttl_ms: a.opt_int("ttl_ms")?,
                pin: pin.as_deref(),
                access: a.access("access")?,
                principal: principal.as_deref(),
                allow_overlap: a.opt_bool("allow_overlap")?.unwrap_or(false),
            };
            S::record(&ops::mount(db, &a.volume("volume")?, &opts)?)
        }
        "unmount" => {
            ops::unmount(db, &mnt()?)?;
            S::unit()
        }
        "renew_mount" => S::record(&ops::renew_mount(db, &mnt()?, a.opt_int("ttl_ms")?)?),
        "mount_info" => S::record(&ops::mount_info(db, &mnt()?)?),
        "list_mounts" => S::record(&ops::list_mounts(
            db,
            a.opt_volume("volume")?.as_ref(),
            a.opt_bool("include_unmounted")?.unwrap_or(false),
        )?),
        // -- structural ----------------------------------------------------
        "create_container" => S::record(&ops::create_container(db, &mnt()?, &path()?)?),
        "create_entry" => S::record(&ops::create_entry(
            db,
            &mnt()?,
            &path()?,
            a.opt_bytes("data")?.as_deref(),
        )?),
        "write_all" => {
            ops::write_all(db, &mnt()?, &path()?, &a.bytes("data")?)?;
            S::unit()
        }
        "append" => S::record(&ops::append(db, &mnt()?, &path()?, &a.bytes("data")?)?),
        "truncate" => {
            ops::truncate(db, &mnt()?, &path()?, a.uint("size")?)?;
            S::unit()
        }
        "write_range" => S::record(&ops::write_range(
            db,
            &mnt()?,
            &path()?,
            a.uint("offset")?,
            &a.bytes("data")?,
        )?),
        "link" => {
            ops::link(db, &mnt()?, &a.str("from")?, &a.str("to")?)?;
            S::unit()
        }
        "create_special" => S::record(&ops::create_special(
            db,
            &mnt()?,
            &path()?,
            a.node_type("type")?,
            &a.opt_bytes("data")?.unwrap_or_default(),
        )?),
        "set_owner" => {
            ops::set_owner(
                db,
                &mnt()?,
                &path()?,
                a.opt_int("uid")?,
                a.opt_int("gid")?,
                a.opt_int("mode")?,
            )?;
            S::unit()
        }
        "set_atime" => {
            ops::set_atime(db, &mnt()?, &a.node("node")?, a.int("ts_ns")?)?;
            S::unit()
        }
        "set_mtime" => {
            ops::set_mtime(db, &mnt()?, &a.node("node")?, a.int("ts_ns")?)?;
            S::unit()
        }
        "set_xattr" => {
            ops::set_xattr(db, &mnt()?, &path()?, &a.str("name")?, &a.bytes("value")?)?;
            S::unit()
        }
        "get_xattr" => match ops::get_xattr(db, &mnt()?, &path()?, &a.str("name")?)? {
            Some(v) => S::bytes(&v),
            None => S::null(),
        },
        "list_xattrs" => S::record(&ops::list_xattrs(db, &mnt()?, &path()?)?),
        "remove_xattr" => S::record(&ops::remove_xattr(db, &mnt()?, &path()?, &a.str("name")?)?),
        "set_metadata" => {
            ops::set_metadata(db, &mnt()?, &path()?, &a.map("metadata")?)?;
            S::unit()
        }
        "set_retention" => {
            ops::set_retention(db, &mnt()?, &path()?, a.opt_int("keep")?)?;
            S::unit()
        }
        "move" => {
            ops::move_(db, &mnt()?, &a.str("from")?, &a.str("to")?)?;
            S::unit()
        }
        "copy" => S::record(&ops::copy(db, &mnt()?, &a.str("from")?, &a.str("to")?)?),
        "rename" => {
            ops::rename(db, &mnt()?, &path()?, &a.str("name")?)?;
            S::unit()
        }
        "remove" => {
            ops::remove(db, &mnt()?, &path()?)?;
            S::unit()
        }
        "remove_recursive" => {
            ops::remove_recursive(db, &mnt()?, &path()?)?;
            S::unit()
        }
        "pack" => S::record(&ops::pack(db, &mnt()?, &path()?)?),
        "unpack" => {
            ops::unpack(db, &mnt()?, &path()?)?;
            S::unit()
        }
        // -- read ----------------------------------------------------------
        "resolve" => S::record(&ops::stat(db, &mnt()?, &path()?)?.id),
        "path_of" => S::record(&ops::path_of(db, &mnt()?, &a.node("node")?)?),
        "stat" => S::record(&ops::stat(db, &mnt()?, &path()?)?),
        "stat_by_id" => S::record(&ops::stat_by_id(db, &mnt()?, &a.node("node")?)?),
        "exists" => S::record(&ops::exists(db, &mnt()?, &path()?)?),
        "list" => S::record(&ops::list(
            db,
            &mnt()?,
            a.opt_str("path")?.as_deref().unwrap_or("/"),
        )?),
        "read_all" => S::bytes(&ops::read_all(db, &mnt()?, &path()?)?),
        // -- locking -------------------------------------------------------
        "lock" => S::record(&ops::lock(db, &mnt()?, &path()?, a.opt_int("ttl_ms")?)?),
        "unlock" => {
            ops::unlock(db, &mnt()?, &a.lock("lock")?)?;
            S::unit()
        }
        "renew_lock" => S::record(&ops::renew_lock(
            db,
            &mnt()?,
            &a.lock("lock")?,
            a.opt_int("ttl_ms")?,
        )?),
        // -- streaming: the opens; the rest act on an fd -------------------
        "open_read" => register::<S>(fds, ops::open_read(db, &mnt()?, &path()?)?),
        "open_write" => register::<S>(
            fds,
            ops::open_write(
                db,
                &mnt()?,
                &path()?,
                a.write_mode("mode")?,
                a.opt_lock("lock")?.as_ref(),
            )?,
        ),
        // -- maintenance ---------------------------------------------------
        "prune" => S::record(&ops::prune(db, a.opt_volume("volume")?.as_ref())?),
        "prune_content" => S::record(&ops::prune_content(db, a.opt_volume("volume")?.as_ref())?),
        "verify" => S::record(&ops::verify(
            db,
            &mnt()?,
            a.opt_bool("deep")?.unwrap_or(false),
        )?),
        "health_check" => S::record(&ops::health_check(db)?),
        other => return Err(FsError::usage(format!("no operation {other:?}"))),
    })
}

/// The reference's rule for a duplicated volume name: the most recently
/// created wins, `created_at` then id, so the answer is stable across
/// implementations whatever the row order.
fn resolve_volume_name(db: &mut Db, name: &str) -> Result<Option<VolumeId>, FsError> {
    Ok(ops::list_volumes(db)?
        .into_iter()
        .filter(|v| v.name.as_deref() == Some(name))
        .max_by(|x, y| (x.created_at, &x.id.0).cmp(&(y.created_at, &y.id.0)))
        .map(|v| v.id))
}
