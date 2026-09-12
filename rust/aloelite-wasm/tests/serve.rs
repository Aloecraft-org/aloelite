//! The message protocol over a `MessageChannel`: the envelope, the error
//! object, bytes as a transferred `Uint8Array`, and the server's close.
#![cfg(all(target_arch = "wasm32", target_os = "unknown"))]

use aloelite_wasm::{Fs, serve};
use js_sys::{Object, Promise, Reflect, Uint8Array};
use wasm_bindgen::prelude::*;
use wasm_bindgen_futures::JsFuture;
use wasm_bindgen_test::wasm_bindgen_test;
use web_sys::{MessageChannel, MessageEvent, MessagePort};

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

fn request(id: f64, op: &str, args: JsValue) -> JsValue {
    obj(&[("id", JsValue::from_f64(id)), ("op", s(op)), ("args", args)])
}

/// Post `message` on `port` and await the next message back.
async fn ask(port: &MessagePort, message: JsValue) -> JsValue {
    let reply = Promise::new(&mut |resolve, _| {
        let handler = Closure::once_into_js(move |event: MessageEvent| {
            resolve.call1(&JsValue::UNDEFINED, &event.data()).unwrap();
        });
        port.set_onmessage(Some(handler.unchecked_ref()));
    });
    port.post_message(&message).unwrap();
    JsFuture::from(reply).await.unwrap()
}

#[wasm_bindgen_test]
async fn requests_are_answered_under_their_id() {
    let channel = MessageChannel::new().unwrap();
    let mut server = serve(Fs::open_memory().unwrap(), channel.port1().into()).unwrap();
    let client = channel.port2();

    let reply = ask(
        &client,
        request(1.0, "create_volume", obj(&[("name", s("v"))])),
    )
    .await;
    assert_eq!(get(&reply, "id").as_f64(), Some(1.0));
    assert!(get(&reply, "error").is_undefined());
    let vid = get(&get(&reply, "ok"), "id").as_string().unwrap();

    let reply = ask(&client, request(2.0, "mount", obj(&[("volume", s(&vid))]))).await;
    let mount = get(&reply, "ok").as_string().expect("MountId");

    let reply = ask(
        &client,
        request(
            3.0,
            "create_entry",
            obj(&[
                ("mount", s(&mount)),
                ("path", s("/a")),
                ("data", s("payload")),
            ]),
        ),
    )
    .await;
    assert!(get(&reply, "ok").as_string().is_some(), "NodeId");

    let reply = ask(
        &client,
        request(
            4.0,
            "read_all",
            obj(&[("mount", s(&mount)), ("path", s("/a"))]),
        ),
    )
    .await;
    let bytes: Uint8Array = get(&reply, "ok").dyn_into().expect("Uint8Array");
    assert_eq!(bytes.to_vec(), b"payload");

    let reply = ask(&client, request(5.0, "list_volumes", JsValue::UNDEFINED)).await;
    assert_eq!(js_sys::Array::from(&get(&reply, "ok")).length(), 1);

    server.close().await.unwrap();
}

#[wasm_bindgen_test]
async fn a_failure_is_a_plain_error_object_with_the_code() {
    let channel = MessageChannel::new().unwrap();
    let mut server = serve(Fs::open_memory().unwrap(), channel.port1().into()).unwrap();
    let client = channel.port2();

    // an engine error keeps its spec code across the channel
    let reply = ask(
        &client,
        request(7.0, "mount", obj(&[("volume", s("not-a-volume"))])),
    )
    .await;
    assert_eq!(get(&reply, "id").as_f64(), Some(7.0));
    assert!(get(&reply, "ok").is_undefined());
    let error = get(&reply, "error");
    assert_eq!(get(&error, "code").as_string().unwrap(), "not_found");
    assert!(get(&error, "message").as_string().is_some());

    // a request with no op is answered, not dropped, and any id is echoed
    let reply = ask(&client, obj(&[("id", s("abc"))])).await;
    assert_eq!(get(&reply, "id").as_string().unwrap(), "abc");
    assert_eq!(
        get(&get(&reply, "error"), "code").as_string().unwrap(),
        "usage"
    );

    // so is one that is not even an object
    let reply = ask(&client, s("hello?")).await;
    assert_eq!(
        get(&get(&reply, "error"), "code").as_string().unwrap(),
        "usage"
    );

    server.close().await.unwrap();
    server.close().await.unwrap(); // idempotent
}

#[wasm_bindgen_test]
fn an_endpoint_without_post_message_is_refused() {
    let err = serve(Fs::open_memory().unwrap(), Object::new().into()).unwrap_err();
    assert_eq!(get(&err, "code").as_string().unwrap(), "usage");
}
