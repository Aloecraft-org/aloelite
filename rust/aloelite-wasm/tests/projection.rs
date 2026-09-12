//! `Fs.call`'s table against `mount-api.yaml`, in both directions: every
//! operation the spec declares is dispatched with exactly the spec's
//! parameter names, and nothing is dispatched that the spec (or the
//! documented extras) does not declare. Runs natively and in the browser —
//! the table is data, it needs no JS.

use std::collections::{BTreeMap, BTreeSet};

use aloelite_core::FsError;
use aloelite_wasm::fs::{EXTRA_OPS, OPS};
use aloelite_wasm::value::EXTRA_CODES;
use serde_norway::Value as Yaml;

const MOUNT_API: &str = include_str!("../../../aloelite/config/mount-api.yaml");

/// The handle's own lifecycle, not messages: `open` is a constructor
/// (`Fs.openMemory`, `Pool.open`); the session `close` is the method, and
/// its name is taken by the streaming `close` in the table.
const HANDLE_OPS: &[&str] = &["open"];

/// The parameter that names the handle itself.
const HANDLE_PARAMS: &[&str] = &["fs"];

/// The `*_by_id` forms the spec promises that neither the reference nor the
/// engine implements yet — `tests/test_spec_projection.py` pins the same
/// gap. This surface dispatches what the engine has; shrink this as each
/// lands and the test says when the table has forgotten one.
const UNIMPLEMENTED_BY_ID: &[&str] = &[
    "list",
    "read_all",
    "move",
    "copy",
    "rename",
    "remove",
    "remove_recursive",
    "pack",
    "unpack",
    "open_read",
    "open_write",
];

#[cfg(all(target_arch = "wasm32", target_os = "unknown"))]
wasm_bindgen_test::wasm_bindgen_test_configure!(run_in_browser);

macro_rules! projection_test {
    (fn $name:ident() $body:block) => {
        #[cfg_attr(not(all(target_arch = "wasm32", target_os = "unknown")), test)]
        #[cfg_attr(
            all(target_arch = "wasm32", target_os = "unknown"),
            wasm_bindgen_test::wasm_bindgen_test
        )]
        fn $name() $body
    };
}

/// Operation name → parameter names, as the spec declares them for a
/// message: `fs` removed, the `*_by_id` variants added with `node` in place
/// of `path`, the two `close`s merged under the one name.
fn declared() -> BTreeMap<String, BTreeSet<String>> {
    let spec: Yaml = serde_norway::from_str(MOUNT_API).expect("mount-api.yaml parses");
    let mut out: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for (_, group) in spec["operations"].as_mapping().expect("operations") {
        let Some(group) = group.as_mapping() else {
            continue;
        };
        for (name, op) in group {
            let name = name.as_str().expect("op names are strings");
            if name == "note" || HANDLE_OPS.contains(&name) {
                continue;
            }
            let params = op["params"]
                .as_mapping()
                .map(|m| {
                    m.keys()
                        .map(|k| k.as_str().unwrap().to_owned())
                        .filter(|k| !HANDLE_PARAMS.contains(&k.as_str()))
                        .collect::<BTreeSet<_>>()
                })
                .unwrap_or_default();
            out.entry(name.to_owned()).or_default().extend(params);
        }
    }
    for op in spec["id_variants"]["applies_to"]
        .as_sequence()
        .expect("applies_to")
    {
        let op = op.as_str().unwrap();
        if UNIMPLEMENTED_BY_ID.contains(&op) {
            continue;
        }
        let params = out[op]
            .iter()
            .map(|p| {
                if p == "path" {
                    "node".to_owned()
                } else {
                    p.clone()
                }
            })
            .collect();
        out.insert(format!("{op}_by_id"), params);
    }
    out
}

projection_test!(
    fn every_spec_operation_is_dispatched_with_the_spec_parameters() {
        let declared = declared();
        let table: BTreeMap<&str, BTreeSet<String>> = OPS
            .iter()
            .map(|o| (o.name, o.args.iter().map(|s| (*s).to_owned()).collect()))
            .collect();
        let want: BTreeSet<&str> = declared.keys().map(String::as_str).collect();
        let have: BTreeSet<&str> = table.keys().copied().collect();
        assert_eq!(
            want, have,
            "operations differ between mount-api.yaml and Fs::OPS"
        );
        for (name, params) in &declared {
            assert_eq!(
                &table[name.as_str()],
                params,
                "{name}: argument names differ from the spec's parameters"
            );
        }
    }
);

projection_test!(
    fn the_extras_are_the_documented_ones_and_outside_the_spec() {
        let declared = declared();
        let extras: BTreeSet<&str> = EXTRA_OPS.iter().map(|o| o.name).collect();
        assert_eq!(extras, BTreeSet::from(["resolve_volume_name"]));
        for extra in extras {
            assert!(
                !declared.contains_key(extra),
                "{extra} is in the spec; it belongs in OPS, not EXTRA_OPS"
            );
        }
    }
);

projection_test!(
    fn no_operation_is_listed_twice() {
        let mut seen = BTreeSet::new();
        for op in OPS.iter().chain(EXTRA_OPS) {
            assert!(seen.insert(op.name), "{} is listed twice", op.name);
            let args: BTreeSet<&&str> = op.args.iter().collect();
            assert_eq!(args.len(), op.args.len(), "{}: duplicate argument", op.name);
        }
    }
);

projection_test!(
    fn the_extra_error_codes_are_outside_the_closed_set() {
        for code in EXTRA_CODES {
            assert!(
                !FsError::CODES.contains(code),
                "{code} is a spec code; it must not be listed as extra"
            );
        }
    }
);
