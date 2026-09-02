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
//! Scaffold: no runner yet. See doc/RUST_PORT.md.
