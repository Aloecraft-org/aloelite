//! The embedded fixtures: every scenario file, and the spec they are
//! checked against.

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

include!(concat!(env!("OUT_DIR"), "/scenario_files.rs"));

/// `aloelite/config/mount-api.yaml`: the operations and the closed error set
/// the fixture checks in `tests/fixtures.rs` hold scenarios against.
pub const MOUNT_API: &str = include_str!("../../../aloelite/config/mount-api.yaml");

/// The storage inspections a runner must answer by reaching past the API
/// (declared in `conformance/README.md`).
pub const INSPECTIONS: &[&str] = &["assert_pool_rows"];

/// The text of one scenario file, by its basename.
pub fn file(name: &str) -> Option<&'static str> {
    FILES
        .iter()
        .find(|(n, _)| *n == name)
        .map(|(_, text)| *text)
}

/// Parse a fixture; a fixture that does not parse is a broken suite.
pub fn parse(name: &str, text: &str) -> serde_norway::Value {
    serde_norway::from_str(text).unwrap_or_else(|e| panic!("{name}: {e}"))
}

/// Every (file name, scenario) pair, in file order.
pub fn all() -> Vec<(&'static str, serde_norway::Value)> {
    let mut out = Vec::new();
    for (name, text) in FILES {
        let doc = parse(name, text);
        let scenarios = doc["scenarios"]
            .as_sequence()
            .unwrap_or_else(|| panic!("{name}: no `scenarios` list"));
        for sc in scenarios {
            out.push((*name, sc.clone()));
        }
    }
    out
}
