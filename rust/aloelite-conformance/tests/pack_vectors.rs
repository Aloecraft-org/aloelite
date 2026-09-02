//! `conformance/vectors/pack-v1.json` against `aloelite_core::pack`.
//!
//! The same file the Python runner (`tests/test_pack_vectors.py`) reads,
//! driven the same way: every `encode` case must produce the reference's
//! bytes exactly and decode back to its nodes; every `decode` case must be
//! refused with the reference's error code or tolerated into the same node
//! list. Nodes are compared in the file's own JSON view (payload bytes as
//! `d_hex`, absent timestamps absent) so both runners compare plain data.

use std::collections::BTreeMap;

use aloelite_conformance::vectors::PACK_V1;
use aloelite_core::pack::{self, PACK_FMT, PACK_VER, PackNode};
use serde_json::{Map, Value};

#[cfg(all(target_arch = "wasm32", target_os = "unknown"))]
wasm_bindgen_test::wasm_bindgen_test_configure!(run_in_browser);

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

fn vectors() -> Value {
    serde_json::from_str(PACK_V1).expect("pack-v1.json parses")
}

/// The JSON view -> codec input.
fn node_from(v: &Value) -> PackNode {
    PackNode {
        p: v["p"].as_i64().expect("p"),
        t: v["t"].as_str().expect("t").to_owned(),
        n: v["n"].as_str().expect("n").to_owned(),
        c: v.get("c").and_then(Value::as_i64),
        m: v.get("m").and_then(Value::as_i64),
        x: v.get("x").and_then(Value::as_object).map(|m| {
            m.iter()
                .map(|(k, val)| {
                    (
                        k.clone(),
                        val.as_str()
                            .expect("metadata values are strings")
                            .to_owned(),
                    )
                })
                .collect::<BTreeMap<_, _>>()
        }),
        d: v.get("d_hex")
            .and_then(Value::as_str)
            .map(|h| hex::decode(h).expect("valid hex")),
    }
}

/// Codec output -> the JSON view.
fn view(n: &PackNode) -> Value {
    let mut m = Map::new();
    m.insert("p".into(), Value::from(n.p));
    m.insert("t".into(), Value::from(n.t.as_str()));
    m.insert("n".into(), Value::from(n.n.as_str()));
    if let Some(c) = n.c {
        m.insert("c".into(), Value::from(c));
    }
    if let Some(mm) = n.m {
        m.insert("m".into(), Value::from(mm));
    }
    if let Some(x) = &n.x
        && !x.is_empty()
    {
        m.insert(
            "x".into(),
            Value::Object(
                x.iter()
                    .map(|(k, v)| (k.clone(), Value::from(v.as_str())))
                    .collect(),
            ),
        );
    }
    if let Some(d) = &n.d {
        m.insert("d_hex".into(), Value::from(hex::encode(d)));
    }
    Value::Object(m)
}

conformance_test!(
    fn constants_match_the_format() {
        let v = vectors();
        assert_eq!(v["pack_fmt"].as_str(), Some(PACK_FMT));
        assert_eq!(v["pack_ver"].as_u64(), Some(u64::from(PACK_VER)));
    }
);

conformance_test!(
    fn encode_is_byte_exact_for_every_case() {
        for case in vectors()["encode"].as_array().unwrap() {
            let name = case["name"].as_str().unwrap();
            let nodes: Vec<PackNode> = case["nodes"]
                .as_array()
                .unwrap()
                .iter()
                .map(node_from)
                .collect();
            assert_eq!(
                hex::encode(pack::encode(&nodes)),
                case["blob"].as_str().unwrap(),
                "{name}"
            );
        }
    }
);

conformance_test!(
    fn encoded_blobs_decode_back_to_their_nodes() {
        for case in vectors()["encode"].as_array().unwrap() {
            let name = case["name"].as_str().unwrap();
            let blob = hex::decode(case["blob"].as_str().unwrap()).unwrap();
            let got: Vec<Value> = pack::decode(&blob)
                .unwrap_or_else(|e| panic!("{name}: {e}"))
                .iter()
                .map(view)
                .collect();
            assert_eq!(Value::Array(got), case["nodes"], "{name}");
        }
    }
);

conformance_test!(
    fn decode_refuses_and_tolerates_as_the_reference_does() {
        for case in vectors()["decode"].as_array().unwrap() {
            let name = case["name"].as_str().unwrap();
            let blob = hex::decode(case["blob"].as_str().unwrap()).unwrap();
            match (
                case.get("error").and_then(Value::as_str),
                pack::decode(&blob),
            ) {
                (Some(code), Err(e)) => assert_eq!(e.code(), Some(code), "{name}"),
                (Some(code), Ok(_)) => panic!("{name}: expected {code}, decoded successfully"),
                (None, Ok(nodes)) => {
                    let got: Vec<Value> = nodes.iter().map(view).collect();
                    assert_eq!(Value::Array(got), case["nodes"], "{name}");
                }
                (None, Err(e)) => panic!("{name}: expected nodes, got {e}"),
            }
        }
    }
);
