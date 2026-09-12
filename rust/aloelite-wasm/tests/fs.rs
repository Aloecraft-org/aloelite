//! `Fs.call` end to end in a browser page: the round trip, the types that
//! cross the boundary, the error shape, and the descriptors the handle
//! holds. No storage — `Fs.openMemory` — so this is the dispatch alone.
#![cfg(all(target_arch = "wasm32", target_os = "unknown"))]

use aloelite_wasm::Fs;
use js_sys::{Array, BigInt, Object, Reflect, Uint8Array};
use wasm_bindgen::{JsCast, JsValue};
use wasm_bindgen_test::wasm_bindgen_test;

wasm_bindgen_test::wasm_bindgen_test_configure!(run_in_browser);

fn obj(fields: &[(&str, JsValue)]) -> JsValue {
    let o = Object::new();
    for (k, v) in fields {
        Reflect::set(&o, &(*k).into(), v).unwrap();
    }
    o.into()
}

fn s(v: &str) -> JsValue {
    JsValue::from_str(v)
}

fn get(v: &JsValue, key: &str) -> JsValue {
    Reflect::get(v, &key.into()).unwrap()
}

fn code_of(err: &JsValue) -> String {
    get(err, "code").as_string().unwrap_or_default()
}

/// A BigInt's decimal text; fails on anything else, which is the point.
fn big(v: &JsValue) -> String {
    assert!(v.is_bigint(), "expected a BigInt, got {v:?}");
    v.unchecked_ref::<BigInt>().to_string(10).unwrap().into()
}

/// A fresh in-memory store with one volume mounted.
fn mounted() -> (Fs, String) {
    let mut fs = Fs::open_memory().unwrap();
    let vol = fs.call("create_volume", obj(&[("name", s("v"))])).unwrap();
    let vid = get(&vol, "id").as_string().unwrap();
    let mount = fs
        .call("mount", obj(&[("volume", s(&vid))]))
        .unwrap()
        .as_string()
        .expect("a MountId is a string");
    (fs, mount)
}

#[wasm_bindgen_test]
async fn a_file_round_trips_and_records_come_back_typed() {
    let (mut fs, m) = mounted();
    let node = fs
        .call(
            "create_entry",
            obj(&[
                ("mount", s(&m)),
                ("path", s("/hello.txt")),
                ("data", s("hello")),
            ]),
        )
        .unwrap();
    assert!(node.as_string().is_some(), "a NodeId is a string");

    let back: Uint8Array = fs
        .call(
            "read_all",
            obj(&[("mount", s(&m)), ("path", s("/hello.txt"))]),
        )
        .unwrap()
        .dyn_into()
        .expect("bytes come back as a Uint8Array");
    assert_eq!(back.to_vec(), b"hello");

    let info = fs
        .call("stat", obj(&[("mount", s(&m)), ("path", s("/hello.txt"))]))
        .unwrap();
    assert_eq!(get(&info, "type").as_string().unwrap(), "entry");
    assert_eq!(big(&get(&info, "size")), "5");
    assert!(
        get(&info, "modified_at").is_bigint(),
        "timestamps are BigInt"
    );
    assert!(get(&info, "uid").is_null(), "an absent optional is null");

    let listing: Array = fs
        .call("list", obj(&[("mount", s(&m))]))
        .unwrap()
        .dyn_into()
        .unwrap();
    assert_eq!(listing.length(), 1);
    assert_eq!(
        get(&listing.get(0), "name").as_string().unwrap(),
        "hello.txt"
    );

    // bytes in as a Uint8Array, a string map in as an object
    fs.call(
        "write_all",
        obj(&[
            ("mount", s(&m)),
            ("path", s("/hello.txt")),
            ("data", Uint8Array::from(&b"bye"[..]).into()),
        ]),
    )
    .unwrap();
    let after = fs
        .call(
            "append",
            obj(&[
                ("mount", s(&m)),
                ("path", s("/hello.txt")),
                ("data", s("!!")),
            ]),
        )
        .unwrap();
    assert!(after.is_bigint(), "an int result is a BigInt");
    fs.call(
        "set_metadata",
        obj(&[
            ("mount", s(&m)),
            ("path", s("/hello.txt")),
            ("metadata", obj(&[("k", s("v"))])),
        ]),
    )
    .unwrap();
    let info = fs
        .call("stat", obj(&[("mount", s(&m)), ("path", s("/hello.txt"))]))
        .unwrap();
    assert_eq!(big(&get(&info, "size")), "5");
    assert_eq!(get(&get(&info, "metadata"), "k").as_string().unwrap(), "v");
    assert_eq!(
        fs.call("exists", obj(&[("mount", s(&m)), ("path", s("/nope"))]))
            .unwrap()
            .as_bool(),
        Some(false)
    );

    fs.call("unmount", obj(&[("mount", s(&m))])).unwrap();
    assert!(!fs.closed());
    fs.close().await.unwrap();
    assert!(fs.closed());
    let err = fs.call("list_volumes", JsValue::UNDEFINED).unwrap_err();
    assert_eq!(code_of(&err), "usage");
    fs.close().await.unwrap(); // idempotent
}

#[wasm_bindgen_test]
fn errors_are_errors_with_the_spec_code() {
    let (mut fs, m) = mounted();
    let err = fs
        .call("stat", obj(&[("mount", s(&m)), ("path", s("/missing"))]))
        .unwrap_err();
    assert!(
        err.dyn_ref::<js_sys::Error>().is_some(),
        "an Error, not a string"
    );
    assert_eq!(code_of(&err), "not_found");
    assert_eq!(get(&err, "name").as_string().unwrap(), "AloeliteError");
    assert_eq!(
        code_of(
            &fs.call("mount", obj(&[("volume", s("not-a-volume"))]))
                .unwrap_err()
        ),
        "not_found"
    );

    // the request itself being wrong is `usage`, never a silent default
    assert_eq!(
        code_of(&fs.call("no_such_op", JsValue::UNDEFINED).unwrap_err()),
        "usage"
    );
    let unknown_arg = fs
        .call(
            "stat",
            obj(&[("mount", s(&m)), ("path", s("/")), ("deep", JsValue::TRUE)]),
        )
        .unwrap_err();
    assert_eq!(
        code_of(&unknown_arg),
        "usage",
        "an unknown argument is refused"
    );
    assert!(
        get(&unknown_arg, "message")
            .as_string()
            .unwrap()
            .contains("deep")
    );
    assert_eq!(
        code_of(
            &fs.call(
                "stat",
                obj(&[("mount", s(&m)), ("path", JsValue::from_f64(3.0))])
            )
            .unwrap_err()
        ),
        "usage"
    );
    assert_eq!(
        code_of(&fs.call("stat", obj(&[("mount", s(&m))])).unwrap_err()),
        "usage",
        "a missing required argument"
    );
    assert_eq!(
        code_of(
            &fs.call(
                "truncate",
                obj(&[
                    ("mount", s(&m)),
                    ("path", s("/")),
                    ("size", JsValue::from_f64(1.5))
                ])
            )
            .unwrap_err()
        ),
        "usage"
    );
    assert_eq!(
        code_of(&fs.call("stat", Array::new().into()).unwrap_err()),
        "usage",
        "args must be an object"
    );
}

#[wasm_bindgen_test]
fn integers_arrive_as_number_or_bigint_and_leave_exact() {
    let (mut fs, m) = mounted();
    let node = fs
        .call(
            "create_entry",
            obj(&[
                ("mount", s(&m)),
                ("path", s("/f")),
                ("data", s("0123456789")),
            ]),
        )
        .unwrap();
    fs.call(
        "truncate",
        obj(&[
            ("mount", s(&m)),
            ("path", s("/f")),
            ("size", JsValue::from_f64(4.0)),
        ]),
    )
    .unwrap();
    fs.call(
        "truncate",
        obj(&[
            ("mount", s(&m)),
            ("path", s("/f")),
            ("size", BigInt::from(2i64).into()),
        ]),
    )
    .unwrap();
    let info = fs
        .call("stat", obj(&[("mount", s(&m)), ("path", s("/f"))]))
        .unwrap();
    assert_eq!(big(&get(&info, "size")), "2");

    // a nanosecond timestamp above 2^53 must survive the trip untouched
    let ts = "1700000000000000001";
    let ts_big = BigInt::new(&JsValue::from_str(ts)).unwrap();
    fs.call(
        "set_mtime",
        obj(&[
            ("mount", s(&m)),
            ("node", node.clone()),
            ("ts_ns", ts_big.into()),
        ]),
    )
    .unwrap();
    let info = fs
        .call("stat", obj(&[("mount", s(&m)), ("path", s("/f"))]))
        .unwrap();
    assert_eq!(big(&get(&info, "modified_at")), ts);

    // the same magnitude as a Number is refused rather than rounded
    let err = fs
        .call(
            "set_mtime",
            obj(&[
                ("mount", s(&m)),
                ("node", node),
                ("ts_ns", JsValue::from_f64(1.7e18)),
            ]),
        )
        .unwrap_err();
    assert_eq!(code_of(&err), "usage");
}

#[wasm_bindgen_test]
async fn streaming_goes_through_descriptors_held_by_the_handle() {
    let (mut fs, m) = mounted();
    fs.call("create_entry", obj(&[("mount", s(&m)), ("path", s("/s"))]))
        .unwrap();
    let d = fs
        .call("open_write", obj(&[("mount", s(&m)), ("path", s("/s"))]))
        .unwrap();
    let fd = get(&d, "fd")
        .as_string()
        .expect("Descriptor.fd is a string");
    assert!(get(&d, "node").as_string().is_some());
    assert_eq!(get(&d, "writable").as_bool(), Some(true));
    let n = fs
        .call("write", obj(&[("fd", s(&fd)), ("data", s("stream"))]))
        .unwrap();
    assert_eq!(big(&n), "6");
    fs.call("close", obj(&[("fd", s(&fd))])).unwrap();
    assert_eq!(
        code_of(
            &fs.call("write", obj(&[("fd", s(&fd)), ("data", s("x"))]))
                .unwrap_err()
        ),
        "usage",
        "a closed fd is forgotten"
    );

    let d = fs
        .call("open_read", obj(&[("mount", s(&m)), ("path", s("/s"))]))
        .unwrap();
    let fd = get(&d, "fd").as_string().unwrap();
    assert_eq!(get(&d, "writable").as_bool(), Some(false));
    let head: Uint8Array = fs
        .call(
            "read",
            obj(&[("fd", s(&fd)), ("len", JsValue::from_f64(3.0))]),
        )
        .unwrap()
        .dyn_into()
        .unwrap();
    assert_eq!(head.to_vec(), b"str");
    assert_eq!(big(&fs.call("tell", obj(&[("fd", s(&fd))])).unwrap()), "3");
    fs.call(
        "seek",
        obj(&[("fd", s(&fd)), ("offset", JsValue::from_f64(0.0))]),
    )
    .unwrap();
    let all: Uint8Array = fs
        .call("read", obj(&[("fd", s(&fd))]))
        .unwrap()
        .dyn_into()
        .unwrap();
    assert_eq!(all.to_vec(), b"stream");
    fs.call("abort", obj(&[("fd", s(&fd))])).unwrap();
    // closing the handle with a descriptor still open is fine: it is aborted
    fs.call("open_read", obj(&[("mount", s(&m)), ("path", s("/s"))]))
        .unwrap();
    fs.close().await.unwrap();
}

#[wasm_bindgen_test]
fn resolve_volume_name_applies_the_reference_rule() {
    let mut fs = Fs::open_memory().unwrap();
    assert!(
        fs.call("resolve_volume_name", obj(&[("name", s("dup"))]))
            .unwrap()
            .is_null()
    );
    fs.call("create_volume", obj(&[("name", s("dup"))]))
        .unwrap();
    fs.call("create_volume", obj(&[("name", s("dup"))]))
        .unwrap();
    fs.call("create_volume", obj(&[("name", s("other"))]))
        .unwrap();
    let volumes: Array = fs
        .call("list_volumes", JsValue::UNDEFINED)
        .unwrap()
        .dyn_into()
        .unwrap();
    assert_eq!(volumes.length(), 3);
    // the rule: among the same-named, max by (created_at, id)
    let expected = volumes
        .iter()
        .filter(|v| get(v, "name").as_string().as_deref() == Some("dup"))
        .map(|v| {
            let created: i64 = big(&get(&v, "created_at")).parse().unwrap();
            (created, get(&v, "id").as_string().unwrap())
        })
        .max()
        .unwrap()
        .1;
    let got = fs
        .call("resolve_volume_name", obj(&[("name", s("dup"))]))
        .unwrap()
        .as_string()
        .unwrap();
    assert_eq!(got, expected);
    assert!(Fs::operations().contains(&"resolve_volume_name".to_owned()));
    assert!(Fs::operations().contains(&"stat".to_owned()));
}
