//! The browser surface's extra error codes, held where the shared
//! projection test can no longer reach them.
//!
//! `aloelite_api`'s own test pins the three codes the dispatch can raise;
//! these are the three this crate adds on top (`busy`, `opfs`, `io`), and
//! the same two things have to be true of them: outside the spec's closed
//! set, and a superset of the dispatch's, since anything `call` can raise
//! reaches a page through this surface.

use aloelite_core::FsError;
use aloelite_wasm::value::EXTRA_CODES;

#[cfg(all(target_arch = "wasm32", target_os = "unknown"))]
wasm_bindgen_test::wasm_bindgen_test_configure!(run_in_browser);

macro_rules! codes_test {
    (fn $name:ident() $body:block) => {
        #[cfg_attr(not(all(target_arch = "wasm32", target_os = "unknown")), test)]
        #[cfg_attr(
            all(target_arch = "wasm32", target_os = "unknown"),
            wasm_bindgen_test::wasm_bindgen_test
        )]
        fn $name() $body
    };
}

codes_test!(
    fn the_browsers_extra_codes_are_outside_the_closed_set_and_cover_the_dispatchs() {
        for code in EXTRA_CODES {
            assert!(
                !FsError::CODES.contains(code),
                "{code} is a spec code; it must not be listed as extra"
            );
        }
        for code in aloelite_api::EXTRA_CODES {
            assert!(
                EXTRA_CODES.contains(code),
                "{code} can reach a page through Fs.call but is not documented here"
            );
        }
    }
);
