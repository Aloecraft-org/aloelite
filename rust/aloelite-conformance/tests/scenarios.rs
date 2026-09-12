//! `conformance/scenarios/*.yaml` against `aloelite_core::ops`: one test per
//! scenario, minted by `build.rs`, named `<file>_<scenario>`.
//!
//! Runs natively under `cargo test` and in a browser under
//! `wasm-bindgen-test` with no change: the fixtures are embedded, the engine
//! takes an opened connection, a clock and an entropy source, and the only
//! platform seam is where the scenario's database file lives.

#[cfg(all(target_arch = "wasm32", target_os = "unknown"))]
wasm_bindgen_test::wasm_bindgen_test_configure!(run_in_browser);

/// One attribute pair per test: `#[test]` everywhere except the browser,
/// where it is `#[wasm_bindgen_test]`.
macro_rules! conformance_test {
    ($(#[$m:meta])* fn $name:ident() $body:block) => {
        $(#[$m])*
        #[cfg_attr(not(all(target_arch = "wasm32", target_os = "unknown")), test)]
        #[cfg_attr(
            all(target_arch = "wasm32", target_os = "unknown"),
            wasm_bindgen_test::wasm_bindgen_test
        )]
        fn $name() $body
    };
}

include!(concat!(env!("OUT_DIR"), "/scenario_tests.rs"));
