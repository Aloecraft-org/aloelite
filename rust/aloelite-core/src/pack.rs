//! The pack blob codec — a CROSS-IMPLEMENTATION CONTRACT (OP-6/OP-7, TX-2).
//!
//! A packed subtree is one MsgPack map, `{fmt: "aloefs.pack", ver: 1,
//! nodes: [...]}`, with `nodes` in TOP-DOWN canonical order (the `subtree`
//! view: depth, edge_id, node_id), one entry per PLACEMENT, parents before
//! children. Each entry's keys are emitted in [`PackNode`]'s field order,
//! optional ones omitted when absent: `p` parent index or -1, `t` type,
//! `n` the effective placement name, `c`/`m` created/modified ns, `x`
//! metadata (keys sorted; only when non-empty), `d` payload bytes (leaves
//! only). Strings are str, payloads are bin, integers take their smallest
//! encoding — which is what `rmp_serde::to_vec_named` and msgpack-python's
//! `packb(use_bin_type=True)` both do, so the two are byte-identical;
//! `conformance/vectors/pack-v1.json` pins it.
//!
//! VERSIONING. `ver` is a gate: a blob written by a newer build is
//! `unsupported`, never read with this build's field set (which would drop
//! whatever the new version added, silently). A malformed or absent version
//! is `corrupt`. Older versions stay readable: every field added after v1 is
//! optional on read. The head is decoded before the body so a newer blob's
//! unknown shapes cannot masquerade as corruption. The v2 scope is D-8.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use crate::errors::{FsError, Result};

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

pub const PACK_FMT: &str = "aloefs.pack";
pub const PACK_VER: u32 = 1;

/// One node entry. Field order is serialization order and part of the byte
/// contract; `c` and `m` are always written (nil when unknown), `x` and `d`
/// only when present, exactly as the reference writes them.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PackNode {
    pub p: i64,
    pub t: String,
    pub n: String,
    #[serde(default)]
    pub c: Option<i64>,
    #[serde(default)]
    pub m: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub x: Option<BTreeMap<String, String>>,
    #[serde(default, skip_serializing_if = "Option::is_none", with = "serde_bytes")]
    pub d: Option<Vec<u8>>,
}

/// Serialize `nodes` (already in canonical order) as a v1 pack blob.
pub fn encode(nodes: &[PackNode]) -> Vec<u8> {
    rmp_serde::to_vec_named(&PackDoc {
        fmt: PACK_FMT,
        ver: PACK_VER,
        nodes,
    })
    .expect("encoding to memory cannot fail")
}

/// Validate a pack blob and return its node list: `corrupt` for anything
/// that is not a well-formed pack of a known shape, `unsupported` for a
/// pack written by a newer build.
pub fn decode(blob: &[u8]) -> Result<Vec<PackNode>> {
    // The reference refuses a top-level array; serde would happily read a
    // struct from one positionally, so check the marker first: fixmap,
    // map16 or map32.
    if !matches!(blob.first(), Some(0x80..=0x8f | 0xde | 0xdf)) {
        return Err(FsError::corrupt("not an aloefs pack blob"));
    }
    let head: PackHead =
        rmp_serde::from_slice(blob).map_err(|_| FsError::corrupt("not an aloefs pack blob"))?;
    if head.fmt.as_deref() != Some(PACK_FMT) {
        return Err(FsError::corrupt("not an aloefs pack blob"));
    }
    let ver = match head.ver {
        Some(v) if v >= 1 => v,
        other => {
            return Err(FsError::corrupt(format!(
                "pack blob has no usable version ({other:?})"
            )));
        }
    };
    if ver > i64::from(PACK_VER) {
        return Err(FsError::unsupported(format!(
            "pack was written by a newer aloelite (pack format v{ver}; this build understands v{PACK_VER}). Upgrade aloelite to unpack it."
        )));
    }
    let body: PackBody = rmp_serde::from_slice(blob)
        .map_err(|e| FsError::corrupt(format!("pack blob is malformed: {e}")))?;
    body.nodes
        .ok_or_else(|| FsError::corrupt("pack blob has no node list"))
}

// ---------------------------------------------------------------------------
// depth: the wire shapes
// ---------------------------------------------------------------------------

#[derive(Serialize)]
struct PackDoc<'a> {
    fmt: &'a str,
    ver: u32,
    nodes: &'a [PackNode],
}

/// The version gate's view: read before anything else.
#[derive(Deserialize)]
struct PackHead {
    #[serde(default)]
    fmt: Option<String>,
    #[serde(default)]
    ver: Option<i64>,
}

#[derive(Deserialize)]
struct PackBody {
    #[serde(default)]
    nodes: Option<Vec<PackNode>>,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn node(p: i64, t: &str, n: &str, c: i64, m: i64) -> PackNode {
        PackNode {
            p,
            t: t.into(),
            n: n.into(),
            c: Some(c),
            m: Some(m),
            x: None,
            d: None,
        }
    }

    #[test]
    fn bytes_match_the_reference_encoder() {
        // msgpack.packb({"fmt": "aloefs.pack", "ver": 1, "nodes": [
        //   {"p": -1, "t": "container", "n": "d", "c": 5, "m": 5},
        //   {"p": 0, "t": "entry", "n": "f", "c": 6, "m": 7, "x": {"k": "v"}, "d": b"hi"}]},
        //   use_bin_type=True)
        let leaf = PackNode {
            x: Some([("k".to_owned(), "v".to_owned())].into_iter().collect()),
            d: Some(b"hi".to_vec()),
            ..node(0, "entry", "f", 6, 7)
        };
        let bytes = encode(&[node(-1, "container", "d", 5, 5), leaf]);
        let expected = concat!(
            "83",
            "a3666d74",
            "ab616c6f6566732e7061636b",
            "a3766572",
            "01",
            "a56e6f646573",
            "92",
            "85",
            "a170",
            "ff",
            "a174",
            "a9636f6e7461696e6572",
            "a16e",
            "a164",
            "a163",
            "05",
            "a16d",
            "05",
            "87",
            "a170",
            "00",
            "a174",
            "a5656e747279",
            "a16e",
            "a166",
            "a163",
            "06",
            "a16d",
            "07",
            "a178",
            "81",
            "a16b",
            "a176",
            "a164",
            "c4026869",
        );
        assert_eq!(hex::encode(&bytes), expected);
        let back = decode(&bytes).unwrap();
        assert_eq!(back.len(), 2);
        assert_eq!(back[1].d.as_deref(), Some(&b"hi"[..]));
    }

    #[test]
    fn the_gate_runs_before_the_body_and_arrays_are_not_maps() {
        // {"fmt": "aloefs.pack", "ver": 2, "nodes": [{"zz": 1}]} -- a v2 node shape we cannot read
        let newer = hex::decode(
            "83a3666d74ab616c6f6566732e7061636ba3766572 02a56e6f64657391 81a27a7a01"
                .replace(' ', ""),
        )
        .unwrap();
        assert_eq!(decode(&newer).err().unwrap().code(), Some("unsupported"));
        // {"fmt": "aloefs.pack", "nodes": []} -- no version at all
        let versionless = hex::decode("82a3666d74ab616c6f6566732e7061636ba56e6f64657390").unwrap();
        assert_eq!(decode(&versionless).err().unwrap().code(), Some("corrupt"));
        // ["aloefs.pack", 1, []] -- positionally a valid struct, but not a pack
        let array = hex::decode("93ab616c6f6566732e7061636b0190").unwrap();
        assert_eq!(decode(&array).err().unwrap().code(), Some("corrupt"));
        assert_eq!(
            decode(b"not msgpack").err().unwrap().code(),
            Some("corrupt")
        );
        assert_eq!(decode(b"").err().unwrap().code(), Some("corrupt"));
    }
}
