//! The command line.
//!
//! The Python CLI (`aloelite/cli.py`, 754 lines) has no spec — it is the one
//! surface the port has neither a contract nor an oracle for. Two honest
//! options, still open: mirror it verb-for-verb by reading the Python, or
//! declare the Rust CLI its own thing and write the contract first. Either
//! way this crate is thin: verbs map onto Mount API operations and nothing
//! here should know anything the spec does not say.
//!
//! Builds for `wasm32-wasip2` as well as native — a WASI component under
//! wasmtime, volume on the host filesystem. No FUSE, no kernel.
//!
//! Scaffold: no verbs yet.
