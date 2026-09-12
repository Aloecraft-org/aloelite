//! `conformance/vectors/ids-v1.json` against `aloelite_core::ids`.
//!
//! The same file the Python runner (`tests/test_ids.py::test_conformance_id_vectors`)
//! reads, driven the same way: assert the deterministic 19-character prefix
//! for each `prefix` case, then drive a fresh `MonotonicMint` through each
//! `mint_sequences` script and pin its `(ts, seq)` after every step. The
//! vectors are parsed as untyped JSON on purpose — steps are heterogeneous
//! objects and the file's shape, not a Rust type, is the contract.
//!
//! Runs natively under `cargo test` and in a browser under
//! `wasm-bindgen-test` with no change: the data is embedded and the mint
//! takes time and randomness as arguments.

use aloelite_conformance::vectors::IDS_V1;
use aloelite_core::ids::{MonotonicMint, format_uuid7};
use ego_platform::entropy::SeededEntropy;
use serde_json::Value;

#[cfg(all(target_arch = "wasm32", target_os = "unknown"))]
wasm_bindgen_test::wasm_bindgen_test_configure!(run_in_browser);

/// One attribute pair per test: `#[test]` everywhere except the browser,
/// where it is `#[wasm_bindgen_test]`. WASI is `#[test]` (a wasmtime runner
/// is a future addition; it is not a browser).
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

/// Deterministic, and deliberately NOT `CryptoRng`: the tail is random and
/// carries no promise, so this is exactly the seam ego-platform says tests
/// substitute through. The vectors never look at the tail.
fn rng() -> SeededEntropy {
    SeededEntropy::from_seed([7u8; 32])
}

fn vectors() -> Value {
    let v: Value = serde_json::from_str(IDS_V1).expect("ids-v1.json parses");
    assert_eq!(v["format"], "aloelite-id-vectors", "wrong vector file");
    assert_eq!(v["version"], 1, "vector schema moved; update this runner");
    v
}

fn u64_of(v: &Value, what: &str) -> u64 {
    v.as_u64()
        .unwrap_or_else(|| panic!("{what}: expected an unsigned integer, got {v}"))
}

conformance_test! {
    fn prefix_is_byte_identical() {
        let v = vectors();
        let cases = v["prefix"].as_array().expect("prefix cases");
        assert!(!cases.is_empty());
        for case in cases {
            let ts = u64_of(&case["ts_ms"], "ts_ms");
            let seq = u64_of(&case["seq"], "seq") as u16;
            let want = case["prefix"].as_str().expect("prefix string");
            let got = format_uuid7(ts, seq, &mut rng());
            assert!(
                got.starts_with(want),
                "ts_ms={ts} seq={seq}: minted {got}, vectors say prefix {want}"
            );
            assert_eq!(got.len(), 36, "{got}");
        }
    }
}

conformance_test! {
    fn mint_sequences_reproduce_the_state_machine() {
        let v = vectors();
        let seqs = v["mint_sequences"].as_array().expect("mint_sequences");
        assert!(!seqs.is_empty());
        for seq_case in seqs {
            let name = seq_case["name"].as_str().expect("sequence name");
            let mut mint = MonotonicMint::new();
            let mut r = rng();
            for step in seq_case["steps"].as_array().expect("steps") {
                if let Some(f) = step.get("fence") {
                    mint.fence(u64_of(&f[0], "fence ts"), u64_of(&f[1], "fence seq") as u16);
                    continue;
                }
                if let Some(at) = step.get("repeat_mint_at_ms") {
                    let at = u64_of(at, "repeat_mint_at_ms");
                    for _ in 0..u64_of(&step["times"], "times") {
                        mint.mint(at, &mut r);
                    }
                } else {
                    mint.mint(u64_of(&step["mint_at_ms"], "mint_at_ms"), &mut r);
                }
                let want = &step["state"];
                let (ts, seq) = mint.state().expect("a mint always leaves state");
                assert_eq!(
                    (ts, u64::from(seq)),
                    (u64_of(&want[0], "state ts"), u64_of(&want[1], "state seq")),
                    "{name}: after {step}"
                );
            }
        }
    }
}

conformance_test! {
    fn every_id_from_a_sequence_sorts_above_the_one_before_it() {
        // Not in the vectors as bytes, but the property the vectors' state
        // machine exists to guarantee (D-2 clause 1, and what NODE-5 leans
        // on): the WHOLE id string orders, not just the prefix.
        let v = vectors();
        for seq_case in v["mint_sequences"].as_array().expect("mint_sequences") {
            let mut mint = MonotonicMint::new();
            let mut r = rng();
            let mut prev: Option<String> = None;
            for step in seq_case["steps"].as_array().expect("steps") {
                if let Some(f) = step.get("fence") {
                    mint.fence(u64_of(&f[0], "fence ts"), u64_of(&f[1], "fence seq") as u16);
                    continue;
                }
                let at = step
                    .get("repeat_mint_at_ms")
                    .or_else(|| step.get("mint_at_ms"))
                    .map(|a| u64_of(a, "mint time"))
                    .expect("a mint step");
                let times = step.get("times").map(|t| u64_of(t, "times")).unwrap_or(1);
                for _ in 0..times {
                    let id = mint.mint(at, &mut r);
                    if let Some(p) = &prev {
                        assert!(*p < id, "{}: {p} !< {id}", seq_case["name"]);
                    }
                    prev = Some(id);
                }
            }
        }
    }
}
