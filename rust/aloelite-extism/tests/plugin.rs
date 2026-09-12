//! The plug-in end to end, minus Extism: request bytes in, reply bytes out,
//! with the real engine behind them.
//!
//! `#[plugin_fn]` wraps an export in Extism's input/output ABI, which needs a
//! host; everything on this side of that wrapper is ordinary Rust, so what
//! runs here is the same `wire::decode` -> `plugin::dispatch` ->
//! `wire::reply` the export runs. `tests/harness/` drives the built `.wasm`
//! through a real Extism host, which is what proves the wrapper.
//!
//! Each test gets its own thread and so its own handle; `fresh` makes that
//! explicit rather than relying on it.

use aloelite_extism::{plugin, wire};
use rmpv::Value as Mp;

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// One `call`, as the export runs it: encode the request, answer it, decode
/// the reply. `Ok` is the operation's result; `Err` is `(code, message)`.
fn call(op: &str, args: Mp) -> Result<Mp, (String, String)> {
    let request = bytes(&map(&[("op", text(op)), ("args", args)]));
    let reply = wire::reply(wire::decode(&request).and_then(|r| plugin::dispatch(&r.op, &r.args)));
    unwrap(&reply)
}

/// A volume store in memory, on this test's own thread.
fn fresh() {
    let _ = plugin::shut();
    plugin::open_in_memory().expect("open in memory");
}

/// A plain volume, mounted; the mount id.
fn mounted() -> String {
    fresh();
    let vol = call("create_volume", map(&[("name", text("v"))])).unwrap();
    let id = field(&vol, "id");
    call("mount", map(&[("volume", id)]))
        .unwrap()
        .as_str()
        .unwrap()
        .to_owned()
}

#[test]
fn a_volume_is_created_written_and_read_back() {
    let m = mounted();
    call(
        "create_container",
        map(&[("mount", text(&m)), ("path", text("/notes"))]),
    )
    .unwrap();
    call(
        "create_entry",
        map(&[
            ("mount", text(&m)),
            ("path", text("/notes/hello.txt")),
            ("data", Mp::Binary(b"the filesystem is one file".to_vec())),
        ]),
    )
    .unwrap();

    let back = call(
        "read_all",
        map(&[("mount", text(&m)), ("path", text("/notes/hello.txt"))]),
    )
    .unwrap();
    assert_eq!(
        back,
        Mp::Binary(b"the filesystem is one file".to_vec()),
        "bytes come back as msgpack bin, not a string or an array of numbers"
    );

    let listing = call(
        "list",
        map(&[("mount", text(&m)), ("path", text("/notes"))]),
    )
    .unwrap();
    let entries = listing.as_array().expect("list is an array");
    assert_eq!(entries.len(), 1);
    assert_eq!(field(&entries[0], "name"), text("hello.txt"));
}

#[test]
fn a_nanosecond_timestamp_survives_the_round_trip_exactly() {
    // The reason the wire is MessagePack and not JSON. This value is odd,
    // past 2^60, and not representable in a double: through a JSON host it
    // would come back rounded to a multiple of 256 ns.
    const TS: i64 = 1_789_243_772_929_739_743;
    assert_ne!(
        TS, TS as f64 as i64,
        "the fixture must not be a safe double"
    );

    let m = mounted();
    let node = call(
        "create_entry",
        map(&[("mount", text(&m)), ("path", text("/f"))]),
    )
    .unwrap();
    call(
        "set_mtime",
        map(&[("mount", text(&m)), ("node", node), ("ts_ns", Mp::from(TS))]),
    )
    .unwrap();

    let info = call("stat", map(&[("mount", text(&m)), ("path", text("/f"))])).unwrap();
    assert_eq!(field(&info, "modified_at"), Mp::from(TS));
}

#[test]
fn an_encrypted_volume_unlocks_with_its_pin_and_not_without() {
    fresh();
    let vol = call(
        "create_volume",
        map(&[
            ("name", text("secret")),
            ("pin", Mp::Binary(b"hunter2".to_vec())),
            ("enc_mode", text("convergent")),
        ]),
    )
    .unwrap();
    let id = field(&vol, "id");

    let wrong = call(
        "mount",
        map(&[
            ("volume", id.clone()),
            ("pin", Mp::Binary(b"wrong".to_vec())),
        ]),
    );
    assert_eq!(wrong.unwrap_err().0, "bad_key");

    let m = call(
        "mount",
        map(&[("volume", id), ("pin", Mp::Binary(b"hunter2".to_vec()))]),
    )
    .unwrap();
    let m = m.as_str().unwrap();
    call(
        "create_entry",
        map(&[
            ("mount", text(m)),
            ("path", text("/x")),
            ("data", text("hi")),
        ]),
    )
    .unwrap();
    let back = call("read_all", map(&[("mount", text(m)), ("path", text("/x"))])).unwrap();
    assert_eq!(back, Mp::Binary(b"hi".to_vec()));
}

#[test]
fn the_strictness_the_wire_promises_is_the_strictness_it_has() {
    let m = mounted();

    // A float where an integer belongs: msgpack has a real integer type, so
    // this is the host's encoder being wrong rather than a coercion to make.
    let e = call(
        "set_mtime",
        map(&[
            ("mount", text(&m)),
            ("node", text("n")),
            ("ts_ns", Mp::F64(1.0)),
        ]),
    )
    .unwrap_err();
    assert_eq!(e.0, "usage");
    assert!(e.1.contains("a msgpack int, not a float"), "{}", e.1);

    // A misspelled optional is an unknown argument, not a silent default.
    let e = call("list", map(&[("mount", text(&m)), ("Path", text("/"))])).unwrap_err();
    assert_eq!(e.0, "usage");
    assert!(e.1.contains("unknown argument"), "{}", e.1);

    // An operation the table does not carry.
    assert_eq!(call("rm_rf", Mp::Nil).unwrap_err().0, "usage");
}

#[test]
fn the_envelope_refuses_what_it_cannot_read() {
    fresh();
    // Not MessagePack at all.
    let e = unwrap(&wire::reply(
        wire::decode(b"not msgpack").map(|_| vec![aloelite_extism::value::NIL]),
    ))
    .unwrap_err();
    assert_eq!(e.0, "usage");

    // A key the envelope does not take, for the reason an argument map
    // refuses one: `arg` would otherwise run the operation with no arguments.
    let body = bytes(&map(&[("op", text("list")), ("arg", Mp::Nil)]));
    let e = unwrap(&wire::reply(
        wire::decode(&body).map(|_| vec![aloelite_extism::value::NIL]),
    ))
    .unwrap_err();
    assert_eq!(e.0, "usage");
    assert!(e.1.contains("unknown key"), "{}", e.1);

    // No `op`.
    let body = bytes(&map(&[("args", Mp::Nil)]));
    assert_eq!(
        unwrap(&wire::reply(
            wire::decode(&body).map(|_| vec![aloelite_extism::value::NIL])
        ))
        .unwrap_err()
        .0,
        "usage"
    );
}

#[test]
fn a_handle_is_opened_once_closed_once_and_then_gone() {
    fresh();
    assert_eq!(
        plugin::open_in_memory().unwrap_err().code(),
        None,
        "displacing an open handle is a usage error, which carries no spec code"
    );
    plugin::shut().unwrap();
    plugin::shut().unwrap(); // idempotent
    let e = call("list_volumes", Mp::Nil).unwrap_err();
    assert_eq!(e.0, "usage");
    assert!(e.1.contains("closed"), "{}", e.1);

    // An instance that closed one volume may open another.
    plugin::open_in_memory().unwrap();
    call("list_volumes", Mp::Nil).unwrap();
}

#[test]
fn every_operation_the_table_carries_is_reachable_by_name() {
    // Not that each one works -- the conformance suite is for that -- but
    // that the plug-in dispatches the same table as every other frontend.
    let names = aloelite_api::Handle::operations();
    assert!(names.contains(&"create_volume".to_owned()));
    assert!(names.contains(&"resolve_volume_name".to_owned()));
    assert_eq!(
        names.len(),
        aloelite_api::OPS.len() + aloelite_api::EXTRA_OPS.len()
    );
}

// ---------------------------------------------------------------------------
// depth: building and reading MessagePack by hand
// ---------------------------------------------------------------------------

fn map(fields: &[(&str, Mp)]) -> Mp {
    Mp::Map(fields.iter().map(|(k, v)| (text(k), v.clone())).collect())
}

fn text(s: &str) -> Mp {
    Mp::String(s.into())
}

fn bytes(v: &Mp) -> Vec<u8> {
    let mut out = Vec::new();
    rmpv::encode::write_value(&mut out, v).unwrap();
    out
}

fn field(v: &Mp, name: &str) -> Mp {
    v.as_map()
        .expect("a record is a map")
        .iter()
        .find(|(k, _)| k.as_str() == Some(name))
        .unwrap_or_else(|| panic!("no field {name} in {v}"))
        .1
        .clone()
}

/// Read the reply envelope: `{ok}` or `{error: {code, message}}`.
fn unwrap(reply: &[u8]) -> Result<Mp, (String, String)> {
    let v = rmpv::decode::read_value(&mut &reply[..]).expect("the reply is MessagePack");
    let m = v.as_map().expect("the reply is a map");
    assert_eq!(m.len(), 1, "the reply carries exactly one of ok / error");
    match m[0].0.as_str() {
        Some("ok") => Ok(m[0].1.clone()),
        Some("error") => {
            let e = &m[0].1;
            Err((
                field(e, "code").as_str().unwrap().to_owned(),
                field(e, "message").as_str().unwrap().to_owned(),
            ))
        }
        other => panic!("unexpected reply key {other:?}"),
    }
}
