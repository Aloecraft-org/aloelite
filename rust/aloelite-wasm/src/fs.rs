//! The engine behind one handle, driven by the spec's operation names.
//!
//! `Fs.call(op, args)` is the whole API: `op` is a name from
//! `mount-api.yaml`, `args` an object keyed by that operation's parameter
//! names, and the result is the operation's return as [`crate::value`]
//! renders it. One entry point rather than fifty methods, for two reasons.
//! The protocol in [`crate::serve`] is then this same call with an envelope
//! around it, so nothing is reachable over messages that is not reachable
//! directly, or the other way round. And the dispatch is a TABLE, [`OPS`],
//! which `tests/projection.rs` holds against the spec in both directions —
//! an operation cannot be added to one and forgotten in the other, and a
//! parameter name cannot drift in silence.
//!
//! Streaming descriptors live in the handle: `open_read` / `open_write`
//! return the spec's `Descriptor` record, and `read` / `write` / `seek` /
//! `tell` / `close` / `abort` take its `fd`. Closing the handle aborts
//! whatever is still open.

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;

use aloelite_core::ops::{self, MountOptions};
use aloelite_core::types::{FdId, NodeId, VolumeId};
use aloelite_core::{Db, Descriptor, FsError};
use ego_platform::entropy::SystemEntropy;
use rusqlite::Connection;
use serde::Serialize;
use wasm_bindgen::prelude::*;

use crate::args::Args;
use crate::value::{self, bytes, record, unit};

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// One operation `Fs.call` accepts: its spec name and the argument names it
/// takes — the spec's parameter names, minus `fs`, which is the handle.
pub struct Op {
    pub name: &'static str,
    pub args: &'static [&'static str],
}

/// Every Mount API operation this surface dispatches, in the spec's groups
/// and order. `open` is not here — it is a constructor (`Fs.openMemory`,
/// `Pool.open`) — and the session `close` is the method; the `close` below
/// is the streaming one.
pub const OPS: &[Op] = &[
    // -- session -----------------------------------------------------------
    Op {
        name: "create_volume",
        args: &["name", "chunk_size", "pin", "enc_mode"],
    },
    Op {
        name: "change_pin",
        args: &["volume", "old_pin", "new_pin"],
    },
    Op {
        name: "list_volumes",
        args: &[],
    },
    Op {
        name: "mount",
        args: &[
            "volume",
            "at",
            "ttl_ms",
            "pin",
            "access",
            "principal",
            "allow_overlap",
        ],
    },
    Op {
        name: "unmount",
        args: &["mount"],
    },
    Op {
        name: "renew_mount",
        args: &["mount", "ttl_ms"],
    },
    Op {
        name: "mount_info",
        args: &["mount"],
    },
    Op {
        name: "list_mounts",
        args: &["volume", "include_unmounted"],
    },
    // -- structural --------------------------------------------------------
    Op {
        name: "create_container",
        args: &["mount", "path"],
    },
    Op {
        name: "create_entry",
        args: &["mount", "path", "data"],
    },
    Op {
        name: "write_all",
        args: &["mount", "path", "data"],
    },
    Op {
        name: "append",
        args: &["mount", "path", "data"],
    },
    Op {
        name: "truncate",
        args: &["mount", "path", "size"],
    },
    Op {
        name: "write_range",
        args: &["mount", "path", "offset", "data"],
    },
    Op {
        name: "link",
        args: &["mount", "from", "to"],
    },
    Op {
        name: "create_special",
        args: &["mount", "path", "type", "data"],
    },
    Op {
        name: "set_owner",
        args: &["mount", "path", "uid", "gid", "mode"],
    },
    Op {
        name: "set_atime",
        args: &["mount", "node", "ts_ns"],
    },
    Op {
        name: "set_xattr",
        args: &["mount", "path", "name", "value"],
    },
    Op {
        name: "get_xattr",
        args: &["mount", "path", "name"],
    },
    Op {
        name: "list_xattrs",
        args: &["mount", "path"],
    },
    Op {
        name: "remove_xattr",
        args: &["mount", "path", "name"],
    },
    Op {
        name: "set_metadata",
        args: &["mount", "path", "metadata"],
    },
    Op {
        name: "set_mtime",
        args: &["mount", "node", "ts_ns"],
    },
    Op {
        name: "set_retention",
        args: &["mount", "path", "keep"],
    },
    Op {
        name: "move",
        args: &["mount", "from", "to"],
    },
    Op {
        name: "copy",
        args: &["mount", "from", "to"],
    },
    Op {
        name: "rename",
        args: &["mount", "path", "name"],
    },
    Op {
        name: "remove",
        args: &["mount", "path"],
    },
    Op {
        name: "remove_recursive",
        args: &["mount", "path"],
    },
    Op {
        name: "pack",
        args: &["mount", "path"],
    },
    Op {
        name: "unpack",
        args: &["mount", "path"],
    },
    // -- read --------------------------------------------------------------
    Op {
        name: "resolve",
        args: &["mount", "path"],
    },
    Op {
        name: "path_of",
        args: &["mount", "node"],
    },
    Op {
        name: "stat",
        args: &["mount", "path"],
    },
    Op {
        name: "stat_by_id",
        args: &["mount", "node"],
    },
    Op {
        name: "exists",
        args: &["mount", "path"],
    },
    Op {
        name: "list",
        args: &["mount", "path"],
    },
    Op {
        name: "read_all",
        args: &["mount", "path"],
    },
    // -- locking -----------------------------------------------------------
    Op {
        name: "lock",
        args: &["mount", "path", "ttl_ms"],
    },
    Op {
        name: "unlock",
        args: &["mount", "lock"],
    },
    Op {
        name: "renew_lock",
        args: &["mount", "lock", "ttl_ms"],
    },
    // -- streaming ---------------------------------------------------------
    Op {
        name: "open_read",
        args: &["mount", "path"],
    },
    Op {
        name: "open_write",
        args: &["mount", "path", "mode", "lock"],
    },
    Op {
        name: "read",
        args: &["fd", "len"],
    },
    Op {
        name: "write",
        args: &["fd", "data"],
    },
    Op {
        name: "seek",
        args: &["fd", "offset", "whence"],
    },
    Op {
        name: "tell",
        args: &["fd"],
    },
    Op {
        name: "close",
        args: &["fd"],
    },
    Op {
        name: "abort",
        args: &["fd"],
    },
    // -- maintenance -------------------------------------------------------
    Op {
        name: "prune",
        args: &["volume"],
    },
    Op {
        name: "prune_content",
        args: &["volume"],
    },
    Op {
        name: "verify",
        args: &["mount", "deep"],
    },
    Op {
        name: "health_check",
        args: &[],
    },
];

/// Operations beyond the spec — each a facade rule the reference applies
/// outside its operation layer, and nothing the engine could not do through
/// [`OPS`]:
///
/// - `resolve_volume_name(name)` → `VolumeId | null`: the reference's rule
///   for a duplicated volume name, the most recently created wins
///   (`created_at`, then id).
pub const EXTRA_OPS: &[Op] = &[Op {
    name: "resolve_volume_name",
    args: &["name"],
}];

/// `create_volume`'s `chunk_size` when the request leaves it out, as the
/// spec declares it.
pub const DEFAULT_CHUNK_SIZE: usize = 1_048_576;

/// What an `on_close` hook hands back: something to wait for before the
/// close is complete (the Web Lock's actual release, for the pool).
pub type AfterClose = Pin<Box<dyn Future<Output = ()>>>;

/// The engine behind one handle.
#[wasm_bindgen]
pub struct Fs {
    db: Option<Db>,
    fds: HashMap<String, Descriptor>,
    on_close: Option<Box<dyn FnOnce() -> AfterClose>>,
}

#[wasm_bindgen]
impl Fs {
    /// A volume store in memory: nothing outlives the handle. For demos,
    /// tests, and a page that keeps its own bytes.
    #[wasm_bindgen(js_name = openMemory)]
    pub fn open_memory() -> Result<Fs, JsValue> {
        let conn = Connection::open_in_memory().map_err(|e| value::throw(&FsError::from(e)))?;
        let db = Db::open(conn, aloelite_store::clock::system_clock(), SystemEntropy)
            .map_err(|e| value::throw(&e))?;
        Ok(Fs::from_db(db))
    }

    /// Run one operation: `op` is a name from [`OPS`] (or [`EXTRA_OPS`]),
    /// `args` an object keyed by its parameter names, or nothing. Throws an
    /// `Error` whose `code` is the spec's error name.
    pub fn call(&mut self, op: &str, args: JsValue) -> Result<JsValue, JsValue> {
        self.dispatch(op, &args).map_err(|e| value::throw(&e))
    }

    /// Abort open descriptors, flush the engine, release the admission
    /// lock; resolves once the lock is actually released, so an `open` of
    /// the same file that follows the `await` will not see `busy`.
    /// Idempotent; every later `call` is a `usage` error.
    pub async fn close(&mut self) -> Result<(), JsValue> {
        let (result, after) = self.shut();
        if let Some(after) = after {
            after.await;
        }
        result.map_err(|e| value::throw(&e))
    }

    /// Whether `close` has run.
    #[wasm_bindgen(getter)]
    pub fn closed(&self) -> bool {
        self.db.is_none()
    }

    /// Every name `call` accepts, for a host building its own wrapper.
    pub fn operations() -> Vec<String> {
        OPS.iter()
            .chain(EXTRA_OPS)
            .map(|o| o.name.to_owned())
            .collect()
    }
}

impl std::fmt::Debug for Fs {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Fs")
            .field("closed", &self.closed())
            .field("open_descriptors", &self.fds.len())
            .finish()
    }
}

impl Fs {
    /// Wrap an opened engine. How `Pool.open` and `openMemory` build one.
    pub fn from_db(db: Db) -> Fs {
        Fs {
            db: Some(db),
            fds: HashMap::new(),
            on_close: None,
        }
    }

    /// Run `f` when the handle closes and wait for what it returns (the
    /// pool hangs the Web Lock's release here).
    pub fn on_close(&mut self, f: impl FnOnce() -> AfterClose + 'static) {
        self.on_close = Some(Box::new(f));
    }

    /// The synchronous part of [`Fs::close`]: the engine is closed when this
    /// returns; the future, if any, is the part still to wait for. What the
    /// server uses so it never holds a borrow across an await.
    pub fn shut(&mut self) -> (Result<(), FsError>, Option<AfterClose>) {
        let Some(mut db) = self.db.take() else {
            return (Ok(()), None);
        };
        for (_, mut d) in self.fds.drain() {
            let _ = d.abort(&mut db);
        }
        let result = db.close();
        let after = self.on_close.take().map(|f| f());
        (result, after)
    }

    /// [`Fs::call`] without the JS error conversion: what the server uses.
    pub fn dispatch(&mut self, op: &str, args: &JsValue) -> Result<JsValue, FsError> {
        let spec = OPS
            .iter()
            .chain(EXTRA_OPS)
            .find(|o| o.name == op)
            .ok_or_else(|| FsError::usage(format!("no operation {op:?}")))?;
        let a = Args::read(op, args)?;
        a.allow(spec.args)?;
        let Fs { db, fds, .. } = self;
        let db = db
            .as_mut()
            .ok_or_else(|| FsError::usage(format!("{op}: this handle is closed")))?;
        if spec.args.first() == Some(&"fd") {
            descriptor_op(op, &a, fds, db)
        } else {
            engine_op(op, &a, db, fds)
        }
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

fn register(fds: &mut HashMap<String, Descriptor>, d: Descriptor) -> JsValue {
    let projected = record(&DescriptorRecord {
        fd: &d.fd,
        node: &d.node,
        writable: d.writable,
    });
    fds.insert(d.fd.0.clone(), d);
    projected
}

fn descriptor_op(
    op: &str,
    a: &Args,
    fds: &mut HashMap<String, Descriptor>,
    db: &mut Db,
) -> Result<JsValue, FsError> {
    let fd = a.str("fd")?;
    let d = fds
        .get_mut(&fd)
        .ok_or_else(|| FsError::usage(format!("{op}: no open descriptor {fd:?}")))?;
    Ok(match op {
        "read" => {
            // a negative len reads to the end, as the reference's read(-1)
            let n = a.opt_int("len")?.filter(|n| *n >= 0).map(|n| n as usize);
            bytes(&d.read(db, n)?)
        }
        "write" => record(&d.write(db, &a.bytes("data")?)?),
        "seek" => record(&d.seek(db, a.int("offset")?, a.whence("whence")?)?),
        "tell" => record(&d.tell()?),
        "close" | "abort" => {
            let result = if op == "close" {
                d.close(db)
            } else {
                d.abort(db)
            };
            fds.remove(&fd);
            result?;
            unit()
        }
        other => {
            return Err(FsError::usage(format!(
                "{other} is not a descriptor operation"
            )));
        }
    })
}

fn engine_op(
    op: &str,
    a: &Args,
    db: &mut Db,
    fds: &mut HashMap<String, Descriptor>,
) -> Result<JsValue, FsError> {
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
            record(&ops::create_volume(
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
            unit()
        }
        "list_volumes" => record(&ops::list_volumes(db)?),
        "resolve_volume_name" => record(&resolve_volume_name(db, &a.str("name")?)?),
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
            record(&ops::mount(db, &a.volume("volume")?, &opts)?)
        }
        "unmount" => {
            ops::unmount(db, &mnt()?)?;
            unit()
        }
        "renew_mount" => record(&ops::renew_mount(db, &mnt()?, a.opt_int("ttl_ms")?)?),
        "mount_info" => record(&ops::mount_info(db, &mnt()?)?),
        "list_mounts" => record(&ops::list_mounts(
            db,
            a.opt_volume("volume")?.as_ref(),
            a.opt_bool("include_unmounted")?.unwrap_or(false),
        )?),
        // -- structural ----------------------------------------------------
        "create_container" => record(&ops::create_container(db, &mnt()?, &path()?)?),
        "create_entry" => record(&ops::create_entry(
            db,
            &mnt()?,
            &path()?,
            a.opt_bytes("data")?.as_deref(),
        )?),
        "write_all" => {
            ops::write_all(db, &mnt()?, &path()?, &a.bytes("data")?)?;
            unit()
        }
        "append" => record(&ops::append(db, &mnt()?, &path()?, &a.bytes("data")?)?),
        "truncate" => {
            ops::truncate(db, &mnt()?, &path()?, a.uint("size")?)?;
            unit()
        }
        "write_range" => record(&ops::write_range(
            db,
            &mnt()?,
            &path()?,
            a.uint("offset")?,
            &a.bytes("data")?,
        )?),
        "link" => {
            ops::link(db, &mnt()?, &a.str("from")?, &a.str("to")?)?;
            unit()
        }
        "create_special" => record(&ops::create_special(
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
            unit()
        }
        "set_atime" => {
            ops::set_atime(db, &mnt()?, &a.node("node")?, a.int("ts_ns")?)?;
            unit()
        }
        "set_mtime" => {
            ops::set_mtime(db, &mnt()?, &a.node("node")?, a.int("ts_ns")?)?;
            unit()
        }
        "set_xattr" => {
            ops::set_xattr(db, &mnt()?, &path()?, &a.str("name")?, &a.bytes("value")?)?;
            unit()
        }
        "get_xattr" => match ops::get_xattr(db, &mnt()?, &path()?, &a.str("name")?)? {
            Some(v) => bytes(&v),
            None => JsValue::NULL,
        },
        "list_xattrs" => record(&ops::list_xattrs(db, &mnt()?, &path()?)?),
        "remove_xattr" => record(&ops::remove_xattr(db, &mnt()?, &path()?, &a.str("name")?)?),
        "set_metadata" => {
            ops::set_metadata(db, &mnt()?, &path()?, &a.map("metadata")?)?;
            unit()
        }
        "set_retention" => {
            ops::set_retention(db, &mnt()?, &path()?, a.opt_int("keep")?)?;
            unit()
        }
        "move" => {
            ops::move_(db, &mnt()?, &a.str("from")?, &a.str("to")?)?;
            unit()
        }
        "copy" => record(&ops::copy(db, &mnt()?, &a.str("from")?, &a.str("to")?)?),
        "rename" => {
            ops::rename(db, &mnt()?, &path()?, &a.str("name")?)?;
            unit()
        }
        "remove" => {
            ops::remove(db, &mnt()?, &path()?)?;
            unit()
        }
        "remove_recursive" => {
            ops::remove_recursive(db, &mnt()?, &path()?)?;
            unit()
        }
        "pack" => record(&ops::pack(db, &mnt()?, &path()?)?),
        "unpack" => {
            ops::unpack(db, &mnt()?, &path()?)?;
            unit()
        }
        // -- read ----------------------------------------------------------
        "resolve" => record(&ops::stat(db, &mnt()?, &path()?)?.id),
        "path_of" => record(&ops::path_of(db, &mnt()?, &a.node("node")?)?),
        "stat" => record(&ops::stat(db, &mnt()?, &path()?)?),
        "stat_by_id" => record(&ops::stat_by_id(db, &mnt()?, &a.node("node")?)?),
        "exists" => record(&ops::exists(db, &mnt()?, &path()?)?),
        "list" => record(&ops::list(
            db,
            &mnt()?,
            a.opt_str("path")?.as_deref().unwrap_or("/"),
        )?),
        "read_all" => bytes(&ops::read_all(db, &mnt()?, &path()?)?),
        // -- locking -------------------------------------------------------
        "lock" => record(&ops::lock(db, &mnt()?, &path()?, a.opt_int("ttl_ms")?)?),
        "unlock" => {
            ops::unlock(db, &mnt()?, &a.lock("lock")?)?;
            unit()
        }
        "renew_lock" => record(&ops::renew_lock(
            db,
            &mnt()?,
            &a.lock("lock")?,
            a.opt_int("ttl_ms")?,
        )?),
        // -- streaming: the opens; the rest act on an fd -------------------
        "open_read" => register(fds, ops::open_read(db, &mnt()?, &path()?)?),
        "open_write" => register(
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
        "prune" => record(&ops::prune(db, a.opt_volume("volume")?.as_ref())?),
        "prune_content" => record(&ops::prune_content(db, a.opt_volume("volume")?.as_ref())?),
        "verify" => record(&ops::verify(
            db,
            &mnt()?,
            a.opt_bool("deep")?.unwrap_or(false),
        )?),
        "health_check" => record(&ops::health_check(db)?),
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
