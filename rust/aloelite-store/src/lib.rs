//! Where a connection comes from.
//!
//! `aloelite-core` executes SQL against a `rusqlite::Connection` and never
//! asks how it was opened. This crate is the answer to that question, once
//! per storage model (doc/DECISIONS.md D-7), and it is the one place in the
//! port where `cfg` is allowed:
//!
//! | model | module | targets | durability |
//! |---|---|---|---|
//! | **file** — a path on a real filesystem | [`file`] | native, `wasm32-wasip2` | per transaction |
//! | **memory image + blob** — the whole volume in a `:memory:` database, loaded from and checkpointed back to one `BlobStore` blob | [`image`] | every target | per checkpoint |
//! | **OPFS pool** — `sqlite-wasm-vfs`'s `sahpool` over the Origin Private File System | [`opfs`] | `wasm32-unknown-unknown`, from a Dedicated Worker only | per transaction |
//!
//! Each opener returns an [`aloelite_core::Db`] ready for the operations in
//! `aloelite_core::ops`, built on the platform clock and entropy from
//! ego-platform; every opener also has a `_with` form that takes an
//! injected clock and generator, which is how a test drives expiry. The
//! mount-row model is the same inside all three; what differs is who owns
//! the bytes and how often they reach durable storage.

pub mod clock;
pub mod error;
pub mod image;

#[cfg(not(all(target_arch = "wasm32", target_os = "unknown")))]
pub mod file;

#[cfg(all(target_arch = "wasm32", target_os = "unknown"))]
pub mod opfs;

pub use aloelite_core::Db;
pub use error::{Result, StoreError};
