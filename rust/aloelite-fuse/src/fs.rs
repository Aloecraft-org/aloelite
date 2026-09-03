//! The `fuser::Filesystem` implementation: every handler `aloelite/fuse.py`
//! implements, and no more, so a volume behaves identically whichever daemon
//! serves it.
//!
//! The engine holds one SQLite connection and is single-writer, so all state
//! lives behind one [`std::sync::Mutex`] and every FUSE request serializes
//! through it — the same single-connection model as the reference daemon's
//! trio loop, and what lets the lease-renewal thread share the connection
//! safely. `fuser` calls handlers through `&self`; the mutex is the interior
//! mutability that bridges that to the engine's `&mut Db`.
//!
//! Handlers deliberately left to their trait defaults, matching the
//! reference so `doc/COMPATIBILITY.md` reads the same for both:
//! `fallocate` (glibc emulates via zero-writes), `lseek`
//! (`SEEK_HOLE`/`SEEK_DATA` degrade to whole-file-is-data), and
//! `getlk`/`setlk` (POSIX locks stay kernel-arbitrated per mount; claiming
//! them would move intra-mount arbitration here too — the cross-mount D-4
//! upgrade is a recorded follow-up).

use std::collections::HashMap;
use std::ffi::OsStr;
use std::sync::Mutex;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use aloelite_core::records::NodeInfo;
use aloelite_core::types::{MountId, NodeId, NodeType, WriteMode};
use aloelite_core::{Db, Descriptor, FsError, Result};
use fuser::{
    Errno, FileAttr, FileHandle, FileType, Filesystem, Generation, INodeNo, LockOwner, OpenFlags,
    ReplyAttr, ReplyCreate, ReplyData, ReplyDirectory, ReplyEmpty, ReplyEntry, ReplyOpen,
    ReplyStatfs, ReplyWrite, ReplyXattr, Request, TimeOrNow, WriteFlags,
};

use crate::errno::errno;
use crate::inode::{ROOT, ino, mode_bits};
use crate::overlay::Overlay;

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------
//
// Entry point: AloeFuse::new, then handed to fuser as the Filesystem.
// Configurable: TTL (kernel attr/entry cache lifetime), APPEND_BATCH.
// Fan-out: the Filesystem trait methods below, each locking `inner` and
// dispatching to an Inner method of the same name; Handle is the per-fd
// state, with one variant per open mode.

/// Attr/entry cache lifetime handed to the kernel. 1s bounds staleness from
/// writers outside this process; our own writes flow through the kernel.
pub const TTL: Duration = Duration::from_secs(1);

/// Commit an `O_APPEND` handle's buffer once it reaches this many bytes.
pub const APPEND_BATCH: usize = 1 << 20;

/// The daemon: engine state behind one lock.
pub struct AloeFuse {
    pub(crate) inner: std::sync::Arc<Mutex<Inner>>,
}

impl AloeFuse {
    /// Wrap an opened engine mounted at `mount`. The root node is resolved
    /// once and pinned to inode 1.
    pub fn new(mut db: Db, mount: MountId) -> Result<AloeFuse> {
        let root = aloelite_core::ops::stat(&mut db, &mount, "/")?.id;
        let mut n = HashMap::new();
        n.insert(ROOT, root);
        Ok(AloeFuse {
            inner: std::sync::Arc::new(Mutex::new(Inner {
                db,
                mount,
                n,
                nlookup: HashMap::new(),
                open: HashMap::new(),
                overlays: HashMap::new(),
                next_fh: 0,
            })),
        })
    }
}

// ---------------------------------------------------------------------------
// depth: per-fd handle state
// ---------------------------------------------------------------------------

/// One open file handle. The overlay behind a `Rw` handle is shared per
/// inode and lives in [`Inner::overlays`], keyed by inode; the handle only
/// names it.
enum Handle {
    /// `O_RDONLY`: a ranged stream reader.
    Read { desc: Descriptor, inode: u64 },
    /// `O_WRONLY | O_TRUNC`: a sequential stream writer.
    Write {
        desc: Descriptor,
        pos: u64,
        inode: u64,
        path: String,
    },
    /// `O_WRONLY | O_APPEND`: a buffered append batcher.
    Append {
        buf: Vec<u8>,
        inode: u64,
        path: String,
    },
    /// `O_RDWR` or a partial `O_WRONLY`: random access over the shared
    /// per-inode [`Overlay`].
    Rw { inode: u64 },
}

// ---------------------------------------------------------------------------
// depth: the engine state and its operations
// ---------------------------------------------------------------------------

pub struct Inner {
    pub(crate) db: Db,
    pub(crate) mount: MountId,
    n: HashMap<u64, NodeId>,
    nlookup: HashMap<u64, u64>,
    open: HashMap<u64, Handle>,
    overlays: HashMap<u64, Overlay>,
    next_fh: u64,
}

impl Inner {
    // -- inode bookkeeping --------------------------------------------------
    fn register(&mut self, node: NodeId) -> u64 {
        let i = ino(node.as_str());
        self.n.insert(i, node);
        i
    }

    fn remember(&mut self, node: NodeId) -> u64 {
        let i = self.register(node);
        *self.nlookup.entry(i).or_insert(0) += 1;
        i
    }

    fn forget(&mut self, i: u64, n: u64) {
        if i == ROOT {
            return;
        }
        let left = self.nlookup.get(&i).copied().unwrap_or(0).saturating_sub(n);
        if left > 0 {
            self.nlookup.insert(i, left);
        } else {
            self.nlookup.remove(&i);
            self.n.remove(&i);
        }
    }

    fn node(&self, i: u64) -> Result<NodeId> {
        self.n
            .get(&i)
            .cloned()
            .ok_or_else(|| FsError::not_found(format!("inode {i}")))
    }

    fn path(&mut self, i: u64) -> Result<String> {
        if i == ROOT {
            return Ok("/".to_owned());
        }
        let node = self.node(i)?;
        aloelite_core::ops::path_of(&mut self.db, &self.mount, &node)
    }

    fn child_path(&mut self, parent: u64, name: &OsStr) -> Result<String> {
        let base = self.path(parent)?;
        let base = base.trim_end_matches('/');
        Ok(format!("{base}/{}", name_str(name)?))
    }

    fn next_fh(&mut self) -> u64 {
        self.next_fh += 1;
        self.next_fh
    }

    // -- shared overlay -----------------------------------------------------
    fn open_overlay(&mut self, inode: u64, path: &str) -> Result<()> {
        if !self.overlays.contains_key(&inode) {
            let ov = Overlay::new(&mut self.db, &self.mount, path)?;
            self.overlays.insert(inode, ov);
        }
        self.overlays.get_mut(&inode).unwrap().refs += 1;
        Ok(())
    }

    fn drop_overlay(&mut self, inode: u64) {
        if let Some(ov) = self.overlays.get_mut(&inode) {
            ov.refs = ov.refs.saturating_sub(1);
            if ov.refs == 0 {
                self.overlays.remove(&inode);
            }
        }
    }

    /// Make everything pending on this inode durable so a reader that goes
    /// straight to committed content sees it: commit append batches, flush
    /// dirty extents. (`Rw` reads overlay in memory and skip this.)
    fn settle(&mut self, inode: u64) -> Result<()> {
        let append_fhs: Vec<u64> = self
            .open
            .iter()
            .filter(|(_, h)| matches!(h, Handle::Append { inode: i, buf, .. } if *i == inode && !buf.is_empty()))
            .map(|(fh, _)| *fh)
            .collect();
        for fh in append_fhs {
            self.commit_append(fh)?;
        }
        if let Some(ov) = self.overlays.get_mut(&inode)
            && ov.is_dirty()
        {
            let mut ov = self.overlays.remove(&inode).unwrap();
            let r = ov.flush(&mut self.db, &self.mount);
            self.overlays.insert(inode, ov);
            r?;
        }
        Ok(())
    }

    fn commit_append(&mut self, fh: u64) -> Result<()> {
        if let Some(Handle::Append { buf, path, .. }) = self.open.get_mut(&fh)
            && !buf.is_empty()
        {
            let data = std::mem::take(buf);
            let path = path.clone();
            aloelite_core::ops::append(&mut self.db, &self.mount, &path, &data)?;
        }
        Ok(())
    }

    // -- attributes ---------------------------------------------------------
    fn attr(&self, inode: u64, info: &NodeInfo) -> FileAttr {
        let is_dir = info.kind == NodeType::Container;
        let legacy_symlink = info.metadata.contains_key("symlink");
        let (kind, default_mode) = match info.kind {
            NodeType::Container => (FileType::Directory, 0o777),
            NodeType::Symlink => (FileType::Symlink, 0o777),
            NodeType::Fifo => (FileType::NamedPipe, 0o666),
            NodeType::Socket => (FileType::Socket, 0o666),
            NodeType::Entry if legacy_symlink => (FileType::Symlink, 0o777),
            NodeType::Entry => (FileType::RegularFile, 0o666),
        };
        let size = if is_dir {
            0
        } else {
            info.size.unwrap_or(0).max(0) as u64
        };
        let mtime = info.modified_at;
        FileAttr {
            ino: INodeNo(inode),
            size,
            blocks: size.div_ceil(512),
            atime: to_systime(info.atime.unwrap_or(mtime)),
            mtime: to_systime(mtime),
            ctime: to_systime(info.ctime.unwrap_or(mtime)),
            crtime: to_systime(info.created_at),
            kind,
            perm: mode_bits(info, default_mode) as u16,
            nlink: if is_dir { 2 } else { info.nlink.max(1) as u32 },
            uid: info.uid.map_or_else(current_uid, |u| u as u32),
            gid: info.gid.map_or_else(current_gid, |g| g as u32),
            rdev: 0,
            blksize: 512,
            flags: 0,
        }
    }

    /// The attr for `inode`, with any unflushed size from open handles
    /// overlaid so `fstat` agrees with what a second fd can read.
    fn getattr(&mut self, inode: u64) -> Result<FileAttr> {
        let node = self.node(inode)?;
        let info = aloelite_core::ops::stat_by_id(&mut self.db, &self.mount, &node)?;
        let mut a = self.attr(inode, &info);
        let mut size = a.size;
        for h in self.open.values() {
            match h {
                Handle::Append { buf, inode: i, .. } if *i == inode => {
                    size += buf.len() as u64;
                }
                Handle::Write { pos, inode: i, .. } if *i == inode => {
                    size = size.max(*pos);
                }
                _ => {}
            }
        }
        if let Some(ov) = self.overlays.get(&inode) {
            size = size.max(ov.size());
        }
        a.size = size;
        a.blocks = size.div_ceil(512);
        Ok(a)
    }

    fn lookup(&mut self, parent: u64, name: &OsStr) -> Result<FileAttr> {
        let nm = name_str(name)?;
        if nm == "." {
            return self.getattr(parent);
        }
        if nm == ".." {
            return self.getattr(ROOT);
        }
        let path = self.child_path(parent, name)?;
        let info = aloelite_core::ops::stat(&mut self.db, &self.mount, &path)?;
        let inode = self.remember(info.id.clone());
        Ok(self.attr(inode, &info))
    }

    // -- directories --------------------------------------------------------
    fn readdir(&mut self, inode: u64, offset: u64, reply: &mut ReplyDirectory) -> Result<()> {
        let path = self.path(inode)?;
        let entries = aloelite_core::ops::list(&mut self.db, &self.mount, &path)?;
        for (i, e) in entries.iter().filter(|e| e.visible).enumerate() {
            if (i as u64) < offset {
                continue;
            }
            let child = self.register(e.node.clone());
            let info = aloelite_core::ops::stat_by_id(&mut self.db, &self.mount, &e.node)?;
            if reply.add(
                INodeNo(child),
                i as u64 + 1,
                self.attr(child, &info).kind,
                &e.name,
            ) {
                break;
            }
        }
        Ok(())
    }

    // -- create / delete ----------------------------------------------------
    fn mkdir(&mut self, parent: u64, name: &OsStr, mode: u32) -> Result<FileAttr> {
        let path = self.child_path(parent, name)?;
        let node = aloelite_core::ops::create_container(&mut self.db, &self.mount, &path)?;
        let perms = mode & 0o7777;
        if perms != 0 && perms != 0o777 {
            // mkdir writes mode to metadata (the reference's shape), so a
            // directory reads back the same permission bits either way.
            let meta =
                std::collections::BTreeMap::from([("mode".to_owned(), format!("{perms:o}"))]);
            aloelite_core::ops::set_metadata(&mut self.db, &self.mount, &path, &meta)?;
        }
        let inode = self.remember(node.clone());
        let info = aloelite_core::ops::stat_by_id(&mut self.db, &self.mount, &node)?;
        Ok(self.attr(inode, &info))
    }

    fn symlink(&mut self, parent: u64, name: &OsStr, target: &[u8]) -> Result<FileAttr> {
        let path = self.child_path(parent, name)?;
        let node = aloelite_core::ops::create_special(
            &mut self.db,
            &self.mount,
            &path,
            NodeType::Symlink,
            target,
        )?;
        let inode = self.remember(node.clone());
        let info = aloelite_core::ops::stat_by_id(&mut self.db, &self.mount, &node)?;
        Ok(self.attr(inode, &info))
    }

    fn link(&mut self, inode: u64, new_parent: u64, new_name: &OsStr) -> Result<FileAttr> {
        let src = self.path(inode)?;
        let dst = self.child_path(new_parent, new_name)?;
        aloelite_core::ops::link(&mut self.db, &self.mount, &src, &dst)?;
        let node = self.node(inode)?;
        let info = aloelite_core::ops::stat_by_id(&mut self.db, &self.mount, &node)?;
        Ok(self.attr(inode, &info))
    }

    /// FIFOs and sockets only; a regular file for `S_IFREG`. Device nodes are
    /// refused by the caller (D-3).
    fn mknod(&mut self, parent: u64, name: &OsStr, mode: u32) -> Result<FileAttr> {
        let path = self.child_path(parent, name)?;
        let fmt = mode & libc::S_IFMT;
        let node = if fmt == libc::S_IFIFO {
            aloelite_core::ops::create_special(
                &mut self.db,
                &self.mount,
                &path,
                NodeType::Fifo,
                &[],
            )?
        } else if fmt == libc::S_IFSOCK {
            aloelite_core::ops::create_special(
                &mut self.db,
                &self.mount,
                &path,
                NodeType::Socket,
                &[],
            )?
        } else {
            aloelite_core::ops::create_entry(&mut self.db, &self.mount, &path, Some(&[]))?
        };
        let perms = (mode & 0o7777) as i64;
        if perms != 0 {
            aloelite_core::ops::set_owner(
                &mut self.db,
                &self.mount,
                &path,
                None,
                None,
                Some(perms),
            )?;
        }
        let inode = self.remember(node.clone());
        let info = aloelite_core::ops::stat_by_id(&mut self.db, &self.mount, &node)?;
        Ok(self.attr(inode, &info))
    }

    fn unlink(&mut self, parent: u64, name: &OsStr) -> Result<()> {
        let path = self.child_path(parent, name)?;
        aloelite_core::ops::remove(&mut self.db, &self.mount, &path)
    }

    fn rename(&mut self, parent: u64, name: &OsStr, newparent: u64, newname: &OsStr) -> Result<()> {
        let src = self.child_path(parent, name)?;
        if parent == newparent {
            aloelite_core::ops::rename(&mut self.db, &self.mount, &src, &name_str(newname)?)
        } else {
            let dst = self.child_path(newparent, newname)?;
            aloelite_core::ops::move_(&mut self.db, &self.mount, &src, &dst)
        }
    }

    // -- xattrs (user.* only) ----------------------------------------------
    fn setxattr(&mut self, inode: u64, name: &OsStr, value: &[u8]) -> Result<()> {
        let name = user_xattr(name)?;
        let path = self.path(inode)?;
        aloelite_core::ops::set_xattr(&mut self.db, &self.mount, &path, &name, value)
    }

    fn getxattr(&mut self, inode: u64, name: &OsStr) -> Result<Vec<u8>> {
        let name = user_xattr(name)?;
        let path = self.path(inode)?;
        aloelite_core::ops::get_xattr(&mut self.db, &self.mount, &path, &name)?
            .ok_or_else(|| FsError::Usage("no such xattr".to_owned()))
    }

    fn listxattr(&mut self, inode: u64) -> Result<Vec<u8>> {
        let path = self.path(inode)?;
        let names = aloelite_core::ops::list_xattrs(&mut self.db, &self.mount, &path)?;
        let mut buf = Vec::new();
        for n in names {
            buf.extend_from_slice(n.as_bytes());
            buf.push(0);
        }
        Ok(buf)
    }

    fn removexattr(&mut self, inode: u64, name: &OsStr) -> Result<bool> {
        let name = user_xattr(name)?;
        let path = self.path(inode)?;
        aloelite_core::ops::remove_xattr(&mut self.db, &self.mount, &path, &name)
    }

    fn readlink(&mut self, inode: u64) -> Result<Vec<u8>> {
        let path = self.path(inode)?;
        aloelite_core::ops::read_all(&mut self.db, &self.mount, &path)
    }

    // -- open / io ----------------------------------------------------------
    fn create(
        &mut self,
        parent: u64,
        name: &OsStr,
        mode: u32,
        flags: i32,
    ) -> Result<(u64, FileAttr)> {
        let path = self.child_path(parent, name)?;
        let node = aloelite_core::ops::create_entry(&mut self.db, &self.mount, &path, Some(&[]))?;
        let perms = (mode & 0o7777) as i64;
        if perms != 0 && perms != 0o666 {
            aloelite_core::ops::set_owner(
                &mut self.db,
                &self.mount,
                &path,
                None,
                None,
                Some(perms),
            )?;
        }
        let inode = self.remember(node.clone());
        let fh = self.open_handle(inode, &path, flags)?;
        let info = aloelite_core::ops::stat_by_id(&mut self.db, &self.mount, &node)?;
        Ok((fh, self.attr(inode, &info)))
    }

    fn open(&mut self, inode: u64, flags: i32) -> Result<u64> {
        let path = self.path(inode)?;
        self.open_handle(inode, &path, flags)
    }

    /// Pick the handle kind from the open flags — the four-way split the
    /// module table documents.
    fn open_handle(&mut self, inode: u64, path: &str, flags: i32) -> Result<u64> {
        let acc = flags & libc::O_ACCMODE;
        let fh = self.next_fh();
        let handle = if acc == libc::O_RDONLY {
            let desc = aloelite_core::ops::open_read(&mut self.db, &self.mount, path)?;
            Handle::Read { desc, inode }
        } else if acc == libc::O_WRONLY && (flags & libc::O_TRUNC) != 0 {
            let desc = aloelite_core::ops::open_write(
                &mut self.db,
                &self.mount,
                path,
                WriteMode::Truncate,
                None,
            )?;
            Handle::Write {
                desc,
                pos: 0,
                inode,
                path: path.to_owned(),
            }
        } else if acc == libc::O_WRONLY && (flags & libc::O_APPEND) != 0 {
            Handle::Append {
                buf: Vec::new(),
                inode,
                path: path.to_owned(),
            }
        } else {
            // O_RDWR, or a plain O_WRONLY partial overwrite: shared overlay
            self.open_overlay(inode, path)?;
            if (flags & libc::O_TRUNC) != 0 {
                let mut ov = self.overlays.remove(&inode).unwrap();
                let r = ov.truncate(&mut self.db, &self.mount, 0);
                self.overlays.insert(inode, ov);
                r?;
            }
            Handle::Rw { inode }
        };
        self.open.insert(fh, handle);
        Ok(fh)
    }

    fn read(&mut self, fh: u64, off: u64, size: u32) -> Result<Vec<u8>> {
        match self.open.get(&fh) {
            Some(Handle::Read { inode, .. }) => {
                let inode = *inode;
                self.settle(inode)?;
                // move the handle out so the descriptor and the engine can be
                // borrowed at once, then put it back — the descriptor carries
                // its own position
                let Some(Handle::Read { mut desc, inode }) = self.open.remove(&fh) else {
                    unreachable!("handle kind is stable for a fh");
                };
                let r = (|| {
                    desc.seek(&mut self.db, off as i64, aloelite_core::types::Whence::Set)?;
                    desc.read(&mut self.db, Some(size as usize))
                })();
                self.open.insert(fh, Handle::Read { desc, inode });
                r
            }
            Some(Handle::Rw { inode, .. }) => {
                let inode = *inode;
                let ov = self
                    .overlays
                    .remove(&inode)
                    .ok_or_else(|| FsError::internal("rw handle without an overlay"))?;
                let r = ov.read(&mut self.db, &self.mount, off, size as usize);
                self.overlays.insert(inode, ov);
                r
            }
            Some(_) => Err(FsError::Unsupported {
                msg: "read on a write-only handle".to_owned(),
            }),
            None => Err(FsError::Usage(format!("no open fd {fh}"))),
        }
    }

    fn write(&mut self, fh: u64, off: u64, data: &[u8]) -> Result<u32> {
        match self.open.get_mut(&fh) {
            Some(Handle::Append { buf, .. }) => {
                buf.extend_from_slice(data);
                let full = buf.len() >= APPEND_BATCH;
                if full {
                    self.commit_append(fh)?;
                }
                Ok(data.len() as u32)
            }
            Some(Handle::Write { pos, .. }) => {
                if off != *pos {
                    return Err(FsError::Unsupported {
                        msg: "non-sequential write on a streaming handle".to_owned(),
                    });
                }
                let Some(Handle::Write {
                    mut desc,
                    mut pos,
                    inode,
                    path,
                }) = self.open.remove(&fh)
                else {
                    unreachable!("handle kind is stable for a fh");
                };
                let r = desc.write(&mut self.db, data);
                if let Ok(n) = &r {
                    pos += *n as u64;
                }
                self.open.insert(
                    fh,
                    Handle::Write {
                        desc,
                        pos,
                        inode,
                        path,
                    },
                );
                Ok(r? as u32)
            }
            Some(Handle::Rw { inode, .. }) => {
                let inode = *inode;
                let mut ov = self
                    .overlays
                    .remove(&inode)
                    .ok_or_else(|| FsError::internal("rw handle without an overlay"))?;
                let r = ov.write(&mut self.db, &self.mount, off, data);
                self.overlays.insert(inode, ov);
                Ok(r? as u32)
            }
            Some(Handle::Read { .. }) => Err(FsError::Unsupported {
                msg: "write on a read-only handle".to_owned(),
            }),
            None => Err(FsError::Usage(format!("no open fd {fh}"))),
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn setattr(
        &mut self,
        inode: u64,
        mode: Option<u32>,
        uid: Option<u32>,
        gid: Option<u32>,
        size: Option<u64>,
        atime: Option<TimeOrNow>,
        mtime: Option<TimeOrNow>,
        fh: Option<u64>,
    ) -> Result<FileAttr> {
        if mode.is_some() || uid.is_some() || gid.is_some() {
            let path = self.path(inode)?;
            aloelite_core::ops::set_owner(
                &mut self.db,
                &self.mount,
                &path,
                uid.map(i64::from),
                gid.map(i64::from),
                mode.map(|m| (m & 0o7777) as i64),
            )?;
        }
        if let Some(t) = mtime {
            let node = self.node(inode)?;
            let ns = time_or_now_ns(t, self.db.now_ns());
            aloelite_core::ops::set_mtime(&mut self.db, &self.mount, &node, ns)?;
        }
        if let Some(t) = atime {
            let node = self.node(inode)?;
            let ns = time_or_now_ns(t, self.db.now_ns());
            aloelite_core::ops::set_atime(&mut self.db, &self.mount, &node, ns)?;
        }
        if let Some(new) = size {
            self.set_size(inode, new, fh)?;
        }
        self.getattr(inode)
    }

    fn set_size(&mut self, inode: u64, new: u64, fh: Option<u64>) -> Result<()> {
        match fh.and_then(|fh| self.open.get(&fh)) {
            Some(Handle::Write { pos, .. }) => {
                // tolerate preallocation past the write position; refuse a
                // real shrink into already-flushed bytes
                if new < *pos {
                    return Err(FsError::Unsupported {
                        msg: "truncate below a streaming write position".to_owned(),
                    });
                }
                Ok(())
            }
            Some(Handle::Rw { .. }) => {
                let mut ov = self
                    .overlays
                    .remove(&inode)
                    .ok_or_else(|| FsError::internal("rw handle without an overlay"))?;
                let r = ov.truncate(&mut self.db, &self.mount, new);
                self.overlays.insert(inode, ov);
                r
            }
            Some(Handle::Read { .. }) => Err(FsError::Unsupported {
                msg: "truncate on a read handle".to_owned(),
            }),
            _ => {
                let path = self.path(inode)?;
                aloelite_core::ops::truncate(&mut self.db, &self.mount, &path, new)
            }
        }
    }

    // -- commit / close -----------------------------------------------------
    fn flush(&mut self, fh: u64) -> Result<()> {
        match self.open.get(&fh) {
            Some(Handle::Rw { inode, .. }) => {
                let inode = *inode;
                if let Some(mut ov) = self.overlays.remove(&inode) {
                    let r = ov.flush(&mut self.db, &self.mount);
                    self.overlays.insert(inode, ov);
                    r?;
                }
                Ok(())
            }
            Some(Handle::Append { .. }) => self.commit_append(fh),
            Some(Handle::Write { .. }) => {
                // commit now, synchronous with the app's close(); but a dup'd
                // fd can keep writing after this FLUSH, so the handle must
                // survive — convert it to random access over the committed
                // state. Descriptor.close() is idempotent, so RELEASE is fine.
                let Some(Handle::Write {
                    desc, inode, path, ..
                }) = self.open.remove(&fh)
                else {
                    unreachable!();
                };
                let mut desc = desc;
                desc.close(&mut self.db)?;
                self.open_overlay(inode, &path)?;
                self.open.insert(fh, Handle::Rw { inode });
                Ok(())
            }
            _ => Ok(()),
        }
    }

    fn release(&mut self, fh: u64) -> Result<()> {
        match self.open.remove(&fh) {
            Some(Handle::Write { mut desc, .. }) => desc.close(&mut self.db),
            Some(Handle::Read { mut desc, .. }) => desc.close(&mut self.db),
            Some(Handle::Append { buf, path, .. }) if !buf.is_empty() => {
                aloelite_core::ops::append(&mut self.db, &self.mount, &path, &buf).map(|_| ())
            }
            Some(Handle::Rw { inode, .. }) => {
                if let Some(mut ov) = self.overlays.remove(&inode) {
                    let r = ov.flush(&mut self.db, &self.mount);
                    self.overlays.insert(inode, ov);
                    r?;
                }
                self.drop_overlay(inode);
                Ok(())
            }
            _ => Ok(()),
        }
    }

    /// Abort open handles, unmount the engine session, and close the engine
    /// (flushing the id-mint high-water mark, D-2). The daemon's clean
    /// teardown, run once on exit; consumes the state.
    pub fn finish(mut self) -> Result<()> {
        for (_, h) in std::mem::take(&mut self.open) {
            match h {
                Handle::Write { mut desc, .. } | Handle::Read { mut desc, .. } => {
                    let _ = desc.abort(&mut self.db);
                }
                _ => {}
            }
        }
        self.overlays.clear();
        aloelite_core::ops::unmount(&mut self.db, &self.mount)?;
        self.db.close()
    }

    /// Best-effort unmount without owning the state, for the teardown path
    /// where an unexpected extra reference still holds the lock.
    pub fn unmount_only(&mut self) {
        let _ = aloelite_core::ops::unmount(&mut self.db, &self.mount);
    }

    /// Renew the mount lease (ACC-3), so a crashed daemon's mount expires
    /// rather than blocking the volume's admission forever.
    pub fn renew(&mut self, ttl_ms: i64) -> Result<()> {
        aloelite_core::ops::renew_mount(&mut self.db, &self.mount, Some(ttl_ms)).map(|_| ())
    }
}

// ---------------------------------------------------------------------------
// depth: the Filesystem trait — lock, dispatch, map errors to errno
// ---------------------------------------------------------------------------

macro_rules! guard {
    ($self:ident) => {
        $self.inner.lock().unwrap_or_else(|e| e.into_inner())
    };
}

impl Filesystem for AloeFuse {
    fn lookup(&self, _req: &Request, parent: INodeNo, name: &OsStr, reply: ReplyEntry) {
        match guard!(self).lookup(parent.0, name) {
            Ok(a) => reply.entry(&TTL, &a, Generation(0)),
            Err(e) => reply.error(err(&e)),
        }
    }

    fn forget(&self, _req: &Request, ino: INodeNo, nlookup: u64) {
        guard!(self).forget(ino.0, nlookup);
    }

    fn getattr(&self, _req: &Request, ino: INodeNo, _fh: Option<FileHandle>, reply: ReplyAttr) {
        match guard!(self).getattr(ino.0) {
            Ok(a) => reply.attr(&TTL, &a),
            Err(e) => reply.error(err(&e)),
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn setattr(
        &self,
        _req: &Request,
        ino: INodeNo,
        mode: Option<u32>,
        uid: Option<u32>,
        gid: Option<u32>,
        size: Option<u64>,
        atime: Option<TimeOrNow>,
        mtime: Option<TimeOrNow>,
        _ctime: Option<SystemTime>,
        fh: Option<FileHandle>,
        _crtime: Option<SystemTime>,
        _chgtime: Option<SystemTime>,
        _bkuptime: Option<SystemTime>,
        _flags: Option<fuser::BsdFileFlags>,
        reply: ReplyAttr,
    ) {
        match guard!(self).setattr(ino.0, mode, uid, gid, size, atime, mtime, fh.map(|f| f.0)) {
            Ok(a) => reply.attr(&TTL, &a),
            Err(e) => reply.error(err(&e)),
        }
    }

    fn readlink(&self, _req: &Request, ino: INodeNo, reply: ReplyData) {
        match guard!(self).readlink(ino.0) {
            Ok(bytes) => reply.data(&bytes),
            Err(e) => reply.error(err(&e)),
        }
    }

    fn mknod(
        &self,
        _req: &Request,
        parent: INodeNo,
        name: &OsStr,
        mode: u32,
        _umask: u32,
        _rdev: u32,
        reply: ReplyEntry,
    ) {
        // device nodes refused by decision (D-3), not omission
        let fmt = mode & libc::S_IFMT;
        if fmt == libc::S_IFBLK || fmt == libc::S_IFCHR {
            return reply.error(Errno::from_i32(libc::EPERM));
        }
        match guard!(self).mknod(parent.0, name, mode) {
            Ok(a) => reply.entry(&TTL, &a, Generation(0)),
            Err(e) => reply.error(err(&e)),
        }
    }

    fn mkdir(
        &self,
        _req: &Request,
        parent: INodeNo,
        name: &OsStr,
        mode: u32,
        _umask: u32,
        reply: ReplyEntry,
    ) {
        match guard!(self).mkdir(parent.0, name, mode) {
            Ok(a) => reply.entry(&TTL, &a, Generation(0)),
            Err(e) => reply.error(err(&e)),
        }
    }

    fn unlink(&self, _req: &Request, parent: INodeNo, name: &OsStr, reply: ReplyEmpty) {
        match guard!(self).unlink(parent.0, name) {
            Ok(()) => reply.ok(),
            Err(e) => reply.error(err(&e)),
        }
    }

    fn rmdir(&self, _req: &Request, parent: INodeNo, name: &OsStr, reply: ReplyEmpty) {
        match guard!(self).unlink(parent.0, name) {
            Ok(()) => reply.ok(),
            Err(e) => reply.error(err(&e)),
        }
    }

    fn symlink(
        &self,
        _req: &Request,
        parent: INodeNo,
        link_name: &OsStr,
        target: &std::path::Path,
        reply: ReplyEntry,
    ) {
        let target = target.as_os_str().as_encoded_bytes().to_vec();
        match guard!(self).symlink(parent.0, link_name, &target) {
            Ok(a) => reply.entry(&TTL, &a, Generation(0)),
            Err(e) => reply.error(err(&e)),
        }
    }

    fn rename(
        &self,
        _req: &Request,
        parent: INodeNo,
        name: &OsStr,
        newparent: INodeNo,
        newname: &OsStr,
        _flags: fuser::RenameFlags,
        reply: ReplyEmpty,
    ) {
        match guard!(self).rename(parent.0, name, newparent.0, newname) {
            Ok(()) => reply.ok(),
            Err(e) => reply.error(err(&e)),
        }
    }

    fn link(
        &self,
        _req: &Request,
        ino: INodeNo,
        newparent: INodeNo,
        newname: &OsStr,
        reply: ReplyEntry,
    ) {
        match guard!(self).link(ino.0, newparent.0, newname) {
            Ok(a) => reply.entry(&TTL, &a, Generation(0)),
            Err(e) => reply.error(err(&e)),
        }
    }

    fn open(&self, _req: &Request, ino: INodeNo, flags: OpenFlags, reply: ReplyOpen) {
        match guard!(self).open(ino.0, flags.0) {
            Ok(fh) => reply.opened(FileHandle(fh), fuser::FopenFlags::empty()),
            Err(e) => reply.error(err(&e)),
        }
    }

    fn create(
        &self,
        _req: &Request,
        parent: INodeNo,
        name: &OsStr,
        mode: u32,
        _umask: u32,
        flags: i32,
        reply: ReplyCreate,
    ) {
        match guard!(self).create(parent.0, name, mode, flags) {
            Ok((fh, a)) => reply.created(
                &TTL,
                &a,
                Generation(0),
                FileHandle(fh),
                fuser::FopenFlags::empty(),
            ),
            Err(e) => reply.error(err(&e)),
        }
    }

    fn read(
        &self,
        _req: &Request,
        _ino: INodeNo,
        fh: FileHandle,
        offset: u64,
        size: u32,
        _flags: OpenFlags,
        _lock_owner: Option<LockOwner>,
        reply: ReplyData,
    ) {
        match guard!(self).read(fh.0, offset, size) {
            Ok(bytes) => reply.data(&bytes),
            Err(e) => reply.error(err(&e)),
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn write(
        &self,
        _req: &Request,
        _ino: INodeNo,
        fh: FileHandle,
        offset: u64,
        data: &[u8],
        _write_flags: WriteFlags,
        _flags: OpenFlags,
        _lock_owner: Option<LockOwner>,
        reply: ReplyWrite,
    ) {
        match guard!(self).write(fh.0, offset, data) {
            Ok(n) => reply.written(n),
            Err(e) => reply.error(err(&e)),
        }
    }

    fn flush(
        &self,
        _req: &Request,
        _ino: INodeNo,
        fh: FileHandle,
        _lock_owner: LockOwner,
        reply: ReplyEmpty,
    ) {
        match guard!(self).flush(fh.0) {
            Ok(()) => reply.ok(),
            Err(e) => reply.error(err(&e)),
        }
    }

    fn fsync(
        &self,
        _req: &Request,
        _ino: INodeNo,
        fh: FileHandle,
        _datasync: bool,
        reply: ReplyEmpty,
    ) {
        match guard!(self).flush(fh.0) {
            Ok(()) => reply.ok(),
            Err(e) => reply.error(err(&e)),
        }
    }

    fn release(
        &self,
        _req: &Request,
        _ino: INodeNo,
        fh: FileHandle,
        _flags: OpenFlags,
        _lock_owner: Option<LockOwner>,
        _flush: bool,
        reply: ReplyEmpty,
    ) {
        match guard!(self).release(fh.0) {
            Ok(()) => reply.ok(),
            Err(e) => reply.error(err(&e)),
        }
    }

    fn opendir(&self, _req: &Request, _ino: INodeNo, _flags: OpenFlags, reply: ReplyOpen) {
        reply.opened(FileHandle(0), fuser::FopenFlags::empty());
    }

    fn readdir(
        &self,
        _req: &Request,
        ino: INodeNo,
        _fh: FileHandle,
        offset: u64,
        mut reply: ReplyDirectory,
    ) {
        match guard!(self).readdir(ino.0, offset, &mut reply) {
            Ok(()) => reply.ok(),
            Err(e) => reply.error(err(&e)),
        }
    }

    fn access(&self, _req: &Request, ino: INodeNo, _mask: fuser::AccessFlags, reply: ReplyEmpty) {
        // world-rw modes are reported in getattr; existence is the check
        let g = guard!(self);
        if ino.0 == ROOT || g.n.contains_key(&ino.0) {
            reply.ok();
        } else {
            reply.error(Errno::from_i32(libc::ENOENT));
        }
    }

    fn setxattr(
        &self,
        _req: &Request,
        ino: INodeNo,
        name: &OsStr,
        value: &[u8],
        _flags: i32,
        _position: u32,
        reply: ReplyEmpty,
    ) {
        match guard!(self).setxattr(ino.0, name, value) {
            Ok(()) => reply.ok(),
            Err(e) => reply.error(err(&e)),
        }
    }

    fn getxattr(&self, _req: &Request, ino: INodeNo, name: &OsStr, size: u32, reply: ReplyXattr) {
        match guard!(self).getxattr(ino.0, name) {
            Ok(bytes) => xattr_reply(reply, &bytes, size),
            // a missing user.* xattr is ENODATA, not the generic map
            Err(FsError::Usage(_)) if user_xattr(name).is_ok() => {
                reply.error(Errno::from_i32(libc::ENODATA))
            }
            Err(e) => reply.error(err(&e)),
        }
    }

    fn listxattr(&self, _req: &Request, ino: INodeNo, size: u32, reply: ReplyXattr) {
        match guard!(self).listxattr(ino.0) {
            Ok(bytes) => xattr_reply(reply, &bytes, size),
            Err(e) => reply.error(err(&e)),
        }
    }

    fn removexattr(&self, _req: &Request, ino: INodeNo, name: &OsStr, reply: ReplyEmpty) {
        match guard!(self).removexattr(ino.0, name) {
            Ok(true) => reply.ok(),
            Ok(false) => reply.error(Errno::from_i32(libc::ENODATA)),
            Err(e) => reply.error(err(&e)),
        }
    }

    fn statfs(&self, _req: &Request, _ino: INodeNo, reply: ReplyStatfs) {
        // a single-file store reports no fixed capacity, like the reference
        reply.statfs(0, 0, 0, 0, 0, 512, 255, 512);
    }
}

// ---------------------------------------------------------------------------
// depth: small conversions
// ---------------------------------------------------------------------------

fn err(e: &FsError) -> Errno {
    Errno::from_i32(errno(e))
}

/// A FUSE name as an engine path segment. Non-UTF-8 names are `EINVAL`: the
/// engine's paths are UTF-8, so there is no faithful representation.
fn name_str(name: &OsStr) -> Result<String> {
    name.to_str()
        .map(str::to_owned)
        .ok_or_else(|| FsError::Usage("non-UTF-8 name".to_owned()))
}

/// A `user.*` xattr name, decoded; any other namespace is `ENOTSUP` (the
/// kernel-enforced namespaces this filesystem does not implement).
fn user_xattr(name: &OsStr) -> Result<String> {
    let decoded = name.to_str().ok_or_else(|| FsError::Unsupported {
        msg: "non-UTF-8 xattr".to_owned(),
    })?;
    if decoded.starts_with("user.") {
        Ok(decoded.to_owned())
    } else {
        Err(FsError::Unsupported {
            msg: format!("xattr namespace {decoded}"),
        })
    }
}

/// The getxattr/listxattr size protocol: a zero `size` asks for the length,
/// otherwise the bytes (or `ERANGE` if they do not fit).
fn xattr_reply(reply: ReplyXattr, bytes: &[u8], size: u32) {
    if size == 0 {
        reply.size(bytes.len() as u32);
    } else if (bytes.len() as u32) <= size {
        reply.data(bytes);
    } else {
        reply.error(Errno::from_i32(libc::ERANGE));
    }
}

fn to_systime(ns: i64) -> SystemTime {
    if ns >= 0 {
        UNIX_EPOCH + Duration::from_nanos(ns as u64)
    } else {
        UNIX_EPOCH
    }
}

fn time_or_now_ns(t: TimeOrNow, now_ns: i64) -> i64 {
    match t {
        TimeOrNow::SpecificTime(st) => st
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos().min(i64::MAX as u128) as i64)
            .unwrap_or(0),
        TimeOrNow::Now => now_ns,
    }
}

fn current_uid() -> u32 {
    // SAFETY: getuid is always safe and never fails.
    unsafe { libc::getuid() }
}

fn current_gid() -> u32 {
    // SAFETY: getgid is always safe and never fails.
    unsafe { libc::getgid() }
}
