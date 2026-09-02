//! The Aloelite engine.
//!
//! This crate is the Rust implementation of `aloelite/config/mount-api.yaml`:
//! the schema (`aloelite/sql/schema.sql`), the sixty templates
//! (`aloelite/config/sql-templates.yaml`), the host-side id mint (D-1/D-2),
//! the ENC-2 key ladder, path resolution, and every operation the Mount API
//! declares. It is the oracle's twin — `conformance/` is what proves the two
//! agree.
//!
//! **The rule this crate lives by:** it compiles to native, `wasm32-wasip2`
//! and `wasm32-unknown-unknown` with zero `cfg`. It performs no I/O of its
//! own: it takes a `rusqlite::Connection` that something else opened, a
//! clock, and an entropy source, and it never asks what platform it is on.
//! Anything that cannot meet that bar belongs in another crate. CI checks it
//! on every push.
//!
//! Scaffold: no engine code yet. See doc/RUST_PORT.md.
