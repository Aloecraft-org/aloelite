//! The conformance runner: the second implementation's inheritance.
//!
//! `conformance/README.md` says it plainly — the Python suite is the
//! reference *implementation's* tests and is not portable; `conformance/`
//! is data, no code, so a second implementation inherits the oracle rather
//! than re-deriving it and hoping. This crate is Rust's runner for that
//! data: scenarios (op sequence -> observable state), vectors (fixed input ->
//! exact bytes), and the harnesses scenarios name.
//!
//! Two properties are non-negotiable:
//!
//! - **It runs on every target.** Scenarios reach the binary via
//!   `include_str!`, not the filesystem, so the same suite runs under
//!   `cargo test` natively and under `wasm-bindgen-test --headless
//!   --firefox` in a browser. A scenario passing on one target and failing
//!   on another is a finding, and the suite exists to produce it.
//! - **It carries the YAML boolean guard.** YAML 1.1 (PyYAML) reads a bare
//!   `on`/`off`/`yes`/`no` key as a boolean; YAML 1.2 (serde_norway) reads
//!   it as a string. The Python runner's `test_no_scenario_key_is_a_yaml_boolean`
//!   must have an equivalent here, or the same fixture means two things in
//!   two implementations — the exact failure conformance/ exists to prevent.
//!
//! See doc/RUST_PORT.md for the plan and what exists.

/// The conformance data, embedded at compile time so the same bytes reach
/// every target — there is no filesystem to read them from in a browser, and
/// a runner that loads files at run time is a runner whose inputs can drift
/// from the tree it was built from.
///
/// Paths are relative to this file: `rust/aloelite-conformance/src/` up to
/// the repository root, then into `conformance/`.
pub mod vectors {
    /// D-1/D-2: the uuid7 layout's deterministic prefix and the
    /// `MonotonicMint` state machine. Runner: `tests/ids_vectors.rs`.
    pub const IDS_V1: &str = include_str!("../../../conformance/vectors/ids-v1.json");

    /// CV-1/CV-2 and the ENC-2 key ladder: fixed inputs to exact bytes.
    /// Runner: not yet written.
    pub const FORMAT_V1: &str = include_str!("../../../conformance/vectors/format-v1.json");
}
