//! Where a connection comes from.
//!
//! `aloelite-core` executes SQL against a `rusqlite::Connection` and never
//! asks how it was opened. This crate is the answer to that question, once
//! per storage model (doc/DECISIONS.md D-7):
//!
//! - **file** — a path on a real filesystem. Native and `wasm32-wasip2`.
//!   The production shape for servers, the CLI, and the FUSE daemon.
//! - **memory image + blob** — the whole volume held in a `:memory:`
//!   database, loaded and persisted as one atomic blob through
//!   `ego_platform::blobs::BlobStore` (`DirStore` natively, `IdbStore` in
//!   the browser, `MemStore` in tests). Every target. The portable shape,
//!   and what the conformance runner uses under `wasm-bindgen-test`.
//! - **browser VFS** — `sqlite-wasm-vfs`'s `sahpool` over OPFS from inside
//!   a Dedicated Worker: real file semantics, per-transaction durability.
//!   `wasm32-unknown-unknown` only. The production shape in the browser.
//!
//! The mount-row model (ACC-1, D-4) is unchanged inside all three; what
//! differs is who owns the bytes and how often they reach durable storage.
//!
//! What exists so far is the platform glue the engine needs before any
//! storage model: [`clock`]. The three models above are the next work. See
//! doc/RUST_PORT.md.

pub mod clock;
