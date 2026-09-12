//! `FsError` → errno, the table `aloelite/fuse.py` keeps as `_ERRNO`, with
//! the same default: anything unlisted is `EIO`, never a silent success.

use aloelite_core::FsError;

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// The errno a caller sees for an engine error.
pub fn errno(e: &FsError) -> i32 {
    use FsError::*;
    match e {
        NotFound { .. } => libc::ENOENT,
        NotAContainer { .. } => libc::ENOTDIR,
        NotAnEntry { .. } => libc::EISDIR,
        NotEmpty { .. } => libc::ENOTEMPTY,
        AlreadyExists { .. } | ContainerExists { .. } => libc::EEXIST,
        Nameless => libc::EINVAL,
        LockHeld { .. } => libc::EAGAIN,
        ReadOnly => libc::EROFS,
        MountConflict { .. } => libc::EBUSY,
        WouldCycle { .. } => libc::EINVAL,
        VolumeMismatch => libc::EXDEV,
        Unsupported { .. } => libc::ENOTSUP,
        // Encryption errors cannot occur at FUSE op time (the volume is
        // already mounted); mapped so nothing is ever swallowed.
        BadKey | EncryptionRequired { .. } => libc::EACCES,
        MountInvalid { .. }
        | MountPointArchived { .. }
        | LockInvalid { .. }
        | Corrupt { .. }
        | Sqlite(_)
        | Internal(_)
        | Usage(_) => libc::EIO,
    }
}
