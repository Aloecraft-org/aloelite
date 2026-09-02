//! `conformance/vectors/pack-v1.json` and `pack-v2.json` against
//! `aloelite_core::pack`.
//!
//! The same files the Python runner (`tests/test_pack_vectors.py`) reads,
//! driven the same way: every v1 `read` case must decode to its nodes (v1 is
//! readable forever); every v2 `encode` case must produce the reference's
//! bytes exactly and decode back; every v2 `decode` case must be refused
//! with the reference's error code or tolerated into the same node list.
//! Nodes are compared in the files' own JSON view (payload and xattr bytes
//! as hex, absent fields absent) so both runners compare plain data.

use std::collections::BTreeMap;

use aloelite_conformance::vectors::{PACK_V1, PACK_V2};
use aloelite_core::pack::{self, Bin, PACK_FMT, PACK_VER, PackNode};
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

fn v1() -> Value {
    serde_json::from_str(PACK_V1).expect("pack-v1.json parses")
}

fn v2() -> Value {
    serde_json::from_str(PACK_V2).expect("pack-v2.json parses")
}

fn opt_i64(v: &Value, key: &str) -> Option<i64> {
    v.get(key).and_then(Value::as_i64)
}

/// The JSON view -> codec input.
fn node_from(v: &Value) -> PackNode {
    PackNode {
        p: v["p"].as_i64().expect("p"),
        t: v["t"].as_str().expect("t").to_owned(),
        n: v["n"].as_str().expect("n").to_owned(),
        c: opt_i64(v, "c"),
        m: opt_i64(v, "m"),
        u: opt_i64(v, "u"),
        g: opt_i64(v, "g"),
        o: opt_i64(v, "o"),
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
        xa: v.get("xa_hex").and_then(Value::as_object).map(|m| {
            m.iter()
                .map(|(k, val)| {
                    (
                        k.clone(),
                        Bin(hex::decode(val.as_str().expect("hex")).expect("valid hex")),
                    )
                })
                .collect::<BTreeMap<_, _>>()
        }),
        rk: opt_i64(v, "rk"),
        d: v.get("d_hex")
            .and_then(Value::as_str)
            .map(|h| Bin(hex::decode(h).expect("valid hex"))),
    }
}

/// Codec output -> the JSON view.
fn view(n: &PackNode) -> Value {
    let mut m = Map::new();
    m.insert("p".into(), Value::from(n.p));
    m.insert("t".into(), Value::from(n.t.as_str()));
    m.insert("n".into(), Value::from(n.n.as_str()));
    for (key, val) in [("c", n.c), ("m", n.m), ("u", n.u), ("g", n.g), ("o", n.o)] {
        if let Some(val) = val {
            m.insert(key.into(), Value::from(val));
        }
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
    if let Some(xa) = &n.xa
        && !xa.is_empty()
    {
        m.insert(
            "xa_hex".into(),
            Value::Object(
                xa.iter()
                    .map(|(k, v)| (k.clone(), Value::from(hex::encode(&v.0))))
                    .collect(),
            ),
        );
    }
    if let Some(rk) = n.rk {
        m.insert("rk".into(), Value::from(rk));
    }
    if let Some(d) = &n.d {
        m.insert("d_hex".into(), Value::from(hex::encode(&d.0)));
    }
    Value::Object(m)
}

fn views(nodes: &[PackNode]) -> Value {
    Value::Array(nodes.iter().map(view).collect())
}

conformance_test!(
    fn constants_match_the_format() {
        assert_eq!(v2()["pack_fmt"].as_str(), Some(PACK_FMT));
        assert_eq!(v2()["pack_ver"].as_u64(), Some(u64::from(PACK_VER)));
        assert_eq!(v1()["pack_ver"].as_u64(), Some(1));
    }
);

conformance_test!(
    fn v1_blobs_still_read() {
        for case in v1()["read"].as_array().unwrap() {
            let name = case["name"].as_str().unwrap();
            let blob = hex::decode(case["blob"].as_str().unwrap()).unwrap();
            let got = pack::decode(&blob).unwrap_or_else(|e| panic!("v1:{name}: {e}"));
            assert_eq!(views(&got), case["nodes"], "v1:{name}");
        }
    }
);

conformance_test!(
    fn encode_is_byte_exact_for_every_case() {
        for case in v2()["encode"].as_array().unwrap() {
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
                "v2:{name}"
            );
        }
    }
);

conformance_test!(
    fn encoded_blobs_decode_back_to_their_nodes() {
        for case in v2()["encode"].as_array().unwrap() {
            let name = case["name"].as_str().unwrap();
            let blob = hex::decode(case["blob"].as_str().unwrap()).unwrap();
            let got = pack::decode(&blob).unwrap_or_else(|e| panic!("v2:{name}: {e}"));
            assert_eq!(views(&got), case["nodes"], "v2:{name}");
        }
    }
);

conformance_test!(
    fn decode_refuses_and_tolerates_as_the_reference_does() {
        for case in v2()["decode"].as_array().unwrap() {
            let name = case["name"].as_str().unwrap();
            let blob = hex::decode(case["blob"].as_str().unwrap()).unwrap();
            match (
                case.get("error").and_then(Value::as_str),
                pack::decode(&blob),
            ) {
                (Some(code), Err(e)) => assert_eq!(e.code(), Some(code), "v2:{name}"),
                (Some(code), Ok(_)) => panic!("v2:{name}: expected {code}, decoded successfully"),
                (None, Ok(nodes)) => assert_eq!(views(&nodes), case["nodes"], "v2:{name}"),
                (None, Err(e)) => panic!("v2:{name}: expected nodes, got {e}"),
            }
        }
    }
);
