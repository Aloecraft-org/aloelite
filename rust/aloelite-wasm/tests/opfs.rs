//! The OPFS pool from a Dedicated Worker: a volume that survives close and
//! reopen, and the Web Lock that makes its file single-writer.
#![cfg(all(target_arch = "wasm32", target_os = "unknown"))]

use aloelite_wasm::pool::Pool;
use aloelite_wasm::{Fs, serve};
use js_sys::{Object, Reflect, Uint8Array};
use wasm_bindgen::{JsCast, JsValue};
use wasm_bindgen_test::wasm_bindgen_test;
use web_sys::MessageChannel;

wasm_bindgen_test::wasm_bindgen_test_configure!(run_in_dedicated_worker);

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

fn options() -> JsValue {
    obj(&[
        ("directory", s("aloelite-wasm-tests")),
        ("vfsName", s("aloelite-wasm-test")),
    ])
}

fn mount(fs: &mut Fs, volume: &str) -> String {
    fs.call("mount", obj(&[("volume", s(volume))]))
        .unwrap()
        .as_string()
        .unwrap()
}

#[wasm_bindgen_test]
async fn a_volume_in_opfs_survives_close_and_reopen() {
    let pool = Pool::install(options()).await.unwrap();
    let _ = pool.delete("reopen.db");

    let mut fs = pool.open("reopen.db".to_owned()).await.unwrap();
    let vid = get(
        &fs.call("create_volume", obj(&[("name", s("v"))])).unwrap(),
        "id",
    )
    .as_string()
    .unwrap();
    let m = mount(&mut fs, &vid);
    fs.call(
        "create_entry",
        obj(&[
            ("mount", s(&m)),
            ("path", s("/hello.txt")),
            ("data", s("hello")),
        ]),
    )
    .unwrap();
    fs.call("unmount", obj(&[("mount", s(&m))])).unwrap();
    fs.close().await.unwrap();
    assert!(pool.exists("reopen.db").unwrap());

    let mut fs = pool.open("reopen.db".to_owned()).await.unwrap();
    let found = fs
        .call("resolve_volume_name", obj(&[("name", s("v"))]))
        .unwrap()
        .as_string()
        .unwrap();
    assert_eq!(found, vid);
    let m = mount(&mut fs, &vid);
    let back: Uint8Array = fs
        .call(
            "read_all",
            obj(&[("mount", s(&m)), ("path", s("/hello.txt"))]),
        )
        .unwrap()
        .dyn_into()
        .unwrap();
    assert_eq!(back.to_vec(), b"hello");
    fs.close().await.unwrap();

    let bytes = pool.export("reopen.db").unwrap().to_vec();
    assert!(
        bytes.starts_with(b"SQLite format 3\0"),
        "export is a whole SQLite file"
    );
    assert!(pool.delete("reopen.db").unwrap());
    assert!(!pool.exists("reopen.db").unwrap());
}

#[wasm_bindgen_test]
async fn a_volume_file_is_single_writer() {
    let pool = Pool::install(options()).await.unwrap();
    let _ = pool.delete("busy.db");

    let mut first = pool.open("busy.db".to_owned()).await.unwrap();
    let err = pool.open("busy.db".to_owned()).await.unwrap_err();
    assert_eq!(code_of(&err), "busy");
    assert!(
        pool.open("other.db".to_owned()).await.is_ok(),
        "a different file is free"
    );
    let _ = pool.delete("other.db");

    // close releases the lock ...
    first.close().await.unwrap();
    let mut second = pool.open("busy.db".to_owned()).await.unwrap();
    second.close().await.unwrap();

    // ... and so does a server's close
    let channel = MessageChannel::new().unwrap();
    let mut server = serve(
        pool.open("busy.db".to_owned()).await.unwrap(),
        channel.port1().into(),
    )
    .unwrap();
    assert_eq!(
        code_of(&pool.open("busy.db".to_owned()).await.unwrap_err()),
        "busy"
    );
    server.close().await.unwrap();
    pool.open("busy.db".to_owned())
        .await
        .unwrap()
        .close()
        .await
        .unwrap();
    let _ = pool.delete("busy.db");
}

#[wasm_bindgen_test]
async fn install_options_are_checked() {
    let err = Pool::install(obj(&[("directroy", s("typo"))]))
        .await
        .unwrap_err();
    assert_eq!(code_of(&err), "usage");
}
