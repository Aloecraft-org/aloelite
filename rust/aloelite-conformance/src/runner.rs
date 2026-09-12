//! Executes one scenario: resolves the fixture vocabulary onto the flat
//! operation layer, and matches what comes back.
//!
//! Deliberately thin, like the Python runner. Its only cleverness is two
//! mappings: the spec's parameter names onto this binding's signatures
//! (`from`/`to` are `src`/`dst`), and the tagged byte forms, so a fixture
//! never has to guess at an encoding. Results are compared as JSON values
//! (every record serializes), bytes as bytes, and descriptors are kept live
//! in the binding table so later steps can drive them.

use std::collections::{BTreeMap, HashMap};

use aloelite_core::ops;
use aloelite_core::types::{LockId, MountId, NodeId, NodeType, Whence, WriteMode};
use aloelite_core::{Db, Descriptor, FsError};
use base64::Engine;
use serde::Serialize;
use serde_json::Value as Json;
use serde_norway::{Mapping, Value as Yaml};

use crate::harness::{Rig, Setup};
use crate::scenarios;
use crate::scratch::Scratch;

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// The spec's parameter names, mapped onto this binding's names.
const ARG_ALIASES: &[(&str, &str)] = &[("from", "src"), ("to", "dst")];

/// The byte tags a fixture may use in a bytes position.
const BYTE_TAGS: &[&str] = &["utf8", "base64", "hex", "repeat"];

/// Run the named scenario from the named file; panics on the first failing
/// step, the way a test should.
pub fn run_scenario(file: &str, name: &str) {
    let text =
        scenarios::file(file).unwrap_or_else(|| panic!("no scenario file {file:?} is embedded"));
    let doc = scenarios::parse(file, text);
    let scenario = doc["scenarios"]
        .as_sequence()
        .and_then(|s| s.iter().find(|sc| sc["scenario"].as_str() == Some(name)))
        .unwrap_or_else(|| panic!("{file}: no scenario {name:?}"));
    run(scenario);
}

/// Run a parsed scenario.
pub fn run(scenario: &Yaml) {
    let name = scenario["scenario"].as_str().expect("scenario name");
    let setup = read_setup(scenario.get("setup"));
    let harness = scenario
        .get("harness")
        .and_then(Yaml::as_str)
        .unwrap_or("default");
    let mut rig = Rig::build(harness, &setup, Scratch::new(name))
        .unwrap_or_else(|| panic!("{name}: harness {harness:?} is not implemented by this runner"));
    let mut binds: HashMap<String, Out> = HashMap::new();
    let steps = scenario["steps"].as_sequence().expect("steps");
    for (i, step) in steps.iter().enumerate() {
        let op = step["op"].as_str().expect("step op");
        let at = format!("{name}: step {i} ({op})");
        let raw_args = step
            .get("args")
            .and_then(Yaml::as_mapping)
            .cloned()
            .unwrap_or_default();
        if scenarios::INSPECTIONS.contains(&op) {
            inspect(op, &raw_args, &rig, &at);
            continue;
        }
        let via = step
            .get("via")
            .and_then(Yaml::as_str)
            .map_or_else(|| rig.default_via().to_owned(), str::to_owned);
        let result = call(op, &raw_args, &mut rig, &via, &mut binds, &at);
        if let Some(code) = step.get("raises").and_then(Yaml::as_str) {
            match result {
                Err(e) => assert_eq!(e.code(), Some(code), "{at}: expected {code}, raised {e:?}"),
                Ok(_) => panic!("{at}: expected {code}, but the operation succeeded"),
            }
            continue;
        }
        let out = result.unwrap_or_else(|e| panic!("{at}: {e:?}"));
        match step.get("bind").and_then(Yaml::as_str) {
            Some(bind) => {
                binds.insert(bind.to_owned(), out);
                if let Some(expected) = step.get("expect") {
                    check(expected, &binds[bind], &binds, &at);
                }
            }
            None => {
                if let Some(expected) = step.get("expect") {
                    check(expected, &out, &binds, &at);
                }
            }
        }
    }
    rig.close();
}

/// What a step produced: a JSON projection of a record or scalar, raw bytes,
/// or a live descriptor remembered with the mount it was opened through.
pub enum Out {
    Json(Json),
    Bytes(Vec<u8>),
    Fd { desc: Descriptor, via: String },
}

// ---------------------------------------------------------------------------
// depth: values
// ---------------------------------------------------------------------------

fn read_setup(setup: Option<&Yaml>) -> Setup {
    let mut out = Setup::default();
    if let Some(cs) = setup
        .and_then(|s| s.get("chunk_size"))
        .and_then(Yaml::as_u64)
    {
        out.chunk_size = cs as usize;
    }
    out
}

/// A tagged byte literal → bytes. The tags are the format's whole encoding
/// story; anything else in a bytes position is a fixture bug.
fn decode(value: &Yaml) -> Vec<u8> {
    let m = value
        .as_mapping()
        .unwrap_or_else(|| panic!("bytes must be tagged, got {value:?}"));
    if let Some(s) = m.get("utf8") {
        return scalar_text(s).into_bytes();
    }
    if let Some(s) = m.get("base64") {
        return base64::engine::general_purpose::STANDARD
            .decode(scalar_text(s))
            .expect("valid base64");
    }
    if let Some(s) = m.get("hex") {
        // whitespace is allowed so long fixtures can group bytes readably
        let compact: String = scalar_text(s).split_whitespace().collect();
        return hex::decode(compact).expect("valid hex");
    }
    if let Some(inner) = m.get("repeat") {
        let count = m
            .get("count")
            .and_then(Yaml::as_u64)
            .expect("repeat needs count") as usize;
        return decode(inner).repeat(count);
    }
    panic!("unknown byte tag in {value:?}");
}

/// YAML scalars in a text position: a bare number or bool still means its
/// text (`{utf8: 1}` is the byte `'1'`), as `str(value)` gives in Python.
fn scalar_text(v: &Yaml) -> String {
    match v {
        Yaml::String(s) => s.clone(),
        Yaml::Bool(b) => if *b { "True" } else { "False" }.to_owned(),
        Yaml::Number(n) => n.to_string(),
        Yaml::Null => "None".to_owned(),
        other => panic!("not a scalar: {other:?}"),
    }
}

fn is_bytes_literal(v: &Yaml) -> bool {
    v.as_mapping()
        .is_some_and(|m| BYTE_TAGS.iter().any(|t| m.contains_key(*t)))
}

fn is_ref(v: &Yaml) -> bool {
    v.as_mapping().is_some_and(|m| m.contains_key("ref"))
}

/// Resolve `{ref: name}` / `{ref: name.field}` against earlier bindings.
fn deref<'a>(v: &Yaml, binds: &'a HashMap<String, Out>) -> Deref<'a> {
    let spec = v["ref"].as_str().expect("ref names a binding");
    let (name, field) = match spec.split_once('.') {
        Some((n, f)) => (n, Some(f)),
        None => (spec, None),
    };
    let target = binds
        .get(name)
        .unwrap_or_else(|| panic!("unbound ref {name:?}"));
    match field {
        None => Deref::Whole(target),
        Some(field) => match target {
            Out::Json(Json::Object(map)) => Deref::Field(
                map.get(field)
                    .unwrap_or_else(|| panic!("{name} has no field {field:?}"))
                    .clone(),
            ),
            Out::Fd { desc, .. } => Deref::Field(match field {
                "fd" => Json::String(desc.fd.0.clone()),
                "node" => Json::String(desc.node.0.clone()),
                "writable" => Json::Bool(desc.writable),
                _ => panic!("descriptor {name} has no field {field:?}"),
            }),
            _ => panic!("{name} is not a record; cannot take .{field}"),
        },
    }
}

enum Deref<'a> {
    Whole(&'a Out),
    Field(Json),
}

/// One resolved argument.
#[derive(Debug, Clone)]
enum Arg {
    Null,
    Bool(bool),
    Int(i64),
    Str(String),
    Bytes(Vec<u8>),
    Map(BTreeMap<String, String>),
}

fn resolve_arg(v: &Yaml, binds: &HashMap<String, Out>) -> Arg {
    if is_bytes_literal(v) {
        return Arg::Bytes(decode(v));
    }
    if is_ref(v) {
        return match deref(v, binds) {
            Deref::Whole(Out::Bytes(b)) => Arg::Bytes(b.clone()),
            Deref::Whole(Out::Json(j)) => json_to_arg(j),
            Deref::Field(j) => json_to_arg(&j),
            Deref::Whole(Out::Fd { .. }) => panic!("a descriptor may only be passed as `fd`"),
        };
    }
    match v {
        Yaml::Null => Arg::Null,
        Yaml::Bool(b) => Arg::Bool(*b),
        Yaml::Number(n) => Arg::Int(n.as_i64().expect("integer argument")),
        Yaml::String(s) => Arg::Str(s.clone()),
        Yaml::Mapping(m) => Arg::Map(
            m.iter()
                .map(|(k, v)| (scalar_text(k), scalar_text(v)))
                .collect(),
        ),
        other => panic!("unsupported argument {other:?}"),
    }
}

fn json_to_arg(j: &Json) -> Arg {
    match j {
        Json::Null => Arg::Null,
        Json::Bool(b) => Arg::Bool(*b),
        Json::Number(n) => Arg::Int(n.as_i64().expect("integer")),
        Json::String(s) => Arg::Str(s.clone()),
        other => panic!("cannot pass {other} as an argument"),
    }
}

/// The step's arguments, resolved and renamed, with typed accessors that
/// fail loudly on a fixture that passes the wrong shape.
struct Args {
    at: String,
    map: BTreeMap<String, Arg>,
}

impl Args {
    fn resolve(raw: &Mapping, binds: &HashMap<String, Out>, at: &str) -> Args {
        let map = raw
            .iter()
            .filter(|(k, _)| k.as_str() != Some("fd"))
            .map(|(k, v)| {
                let key = k.as_str().expect("argument names are strings");
                let key = ARG_ALIASES
                    .iter()
                    .find(|(spec, _)| *spec == key)
                    .map_or(key, |(_, ours)| *ours);
                (key.to_owned(), resolve_arg(v, binds))
            })
            .collect();
        Args {
            at: at.to_owned(),
            map,
        }
    }

    /// Refuse arguments the operation does not take (the Python runner
    /// checks the same against the function signature).
    fn allow(&self, names: &[&str]) {
        let unknown: Vec<&String> = self
            .map
            .keys()
            .filter(|k| !names.contains(&k.as_str()))
            .collect();
        assert!(unknown.is_empty(), "{}: unknown args {unknown:?}", self.at);
    }

    fn get(&self, name: &str) -> Option<&Arg> {
        match self.map.get(name) {
            Some(Arg::Null) | None => None,
            Some(a) => Some(a),
        }
    }

    fn str(&self, name: &str) -> &str {
        self.opt_str(name)
            .unwrap_or_else(|| panic!("{}: missing {name}", self.at))
    }

    fn opt_str(&self, name: &str) -> Option<&str> {
        self.get(name).map(|a| match a {
            Arg::Str(s) => s.as_str(),
            other => panic!("{}: {name} should be a string, got {other:?}", self.at),
        })
    }

    fn bytes(&self, name: &str) -> Vec<u8> {
        self.opt_bytes(name)
            .unwrap_or_else(|| panic!("{}: missing {name}", self.at))
    }

    fn opt_bytes(&self, name: &str) -> Option<Vec<u8>> {
        self.get(name).map(|a| match a {
            Arg::Bytes(b) => b.clone(),
            other => panic!("{}: {name} should be tagged bytes, got {other:?}", self.at),
        })
    }

    fn int(&self, name: &str) -> i64 {
        self.opt_int(name)
            .unwrap_or_else(|| panic!("{}: missing {name}", self.at))
    }

    fn opt_int(&self, name: &str) -> Option<i64> {
        self.get(name).map(|a| match a {
            Arg::Int(i) => *i,
            other => panic!("{}: {name} should be an integer, got {other:?}", self.at),
        })
    }

    fn opt_bool(&self, name: &str) -> Option<bool> {
        self.get(name).map(|a| match a {
            Arg::Bool(b) => *b,
            other => panic!("{}: {name} should be a bool, got {other:?}", self.at),
        })
    }

    fn map(&self, name: &str) -> BTreeMap<String, String> {
        match self.get(name) {
            Some(Arg::Map(m)) => m.clone(),
            None => BTreeMap::new(),
            other => panic!("{}: {name} should be a map, got {other:?}", self.at),
        }
    }

    fn node(&self, name: &str) -> NodeId {
        NodeId(self.str(name).to_owned())
    }

    fn opt_lock(&self, name: &str) -> Option<LockId> {
        self.opt_str(name).map(|s| LockId(s.to_owned()))
    }

    fn lock(&self, name: &str) -> LockId {
        LockId(self.str(name).to_owned())
    }

    fn node_type(&self, name: &str) -> NodeType {
        let s = self.str(name);
        NodeType::parse(s).unwrap_or_else(|| panic!("{}: unknown node type {s:?}", self.at))
    }

    fn write_mode(&self, name: &str) -> WriteMode {
        self.opt_str(name).map_or(WriteMode::Truncate, |s| {
            WriteMode::parse(s).unwrap_or_else(|| panic!("{}: unknown mode {s:?}", self.at))
        })
    }

    fn whence(&self, name: &str) -> Whence {
        self.opt_str(name).map_or(Whence::Set, |s| {
            Whence::parse(s).unwrap_or_else(|| panic!("{}: unknown whence {s:?}", self.at))
        })
    }
}

// ---------------------------------------------------------------------------
// depth: dispatch
// ---------------------------------------------------------------------------

fn json(v: impl Serialize) -> Out {
    Out::Json(serde_json::to_value(v).expect("records serialize"))
}

fn unit() -> Out {
    Out::Json(Json::Null)
}

type Step = Result<Out, FsError>;

fn call(
    op: &str,
    raw: &Mapping,
    rig: &mut Rig,
    via: &str,
    binds: &mut HashMap<String, Out>,
    at: &str,
) -> Step {
    let a = Args::resolve(raw, binds, at);
    // Streaming ops act on a bound descriptor, not on (db, mount) -- the same
    // projection mount-api.yaml's `streaming` note describes for every binding.
    if let Some(fd) = raw.get("fd") {
        let name = fd["ref"]
            .as_str()
            .unwrap_or_else(|| panic!("{at}: fd must be a ref"));
        let Some(Out::Fd { desc, via }) = binds.get_mut(name) else {
            panic!("{at}: {name:?} is not a bound descriptor");
        };
        let db = rig
            .db_for(via)
            .unwrap_or_else(|| panic!("{at}: no mount {via:?}"));
        return descriptor_op(op, &a, desc, db);
    }
    let (db, mount) = rig
        .target(via)
        .unwrap_or_else(|| panic!("{at}: no mount named {via:?} in this harness"));
    mount_op(op, &a, db, &mount, via)
}

fn descriptor_op(op: &str, a: &Args, desc: &mut Descriptor, db: &mut Db) -> Step {
    match op {
        "read" => {
            a.allow(&["len"]);
            let n = a.opt_int("len").filter(|n| *n >= 0).map(|n| n as usize);
            desc.read(db, n).map(Out::Bytes)
        }
        "write" => {
            a.allow(&["data"]);
            desc.write(db, &a.bytes("data")).map(json)
        }
        "seek" => {
            a.allow(&["offset", "whence"]);
            desc.seek(db, a.int("offset"), a.whence("whence")).map(json)
        }
        "tell" => {
            a.allow(&[]);
            desc.tell().map(json)
        }
        "close" => {
            a.allow(&[]);
            desc.close(db).map(|()| unit())
        }
        "abort" => {
            a.allow(&[]);
            desc.abort(db).map(|()| unit())
        }
        other => panic!("{}: {other} is not a descriptor operation", a.at),
    }
}

fn mount_op(op: &str, a: &Args, db: &mut Db, mount: &MountId, via: &str) -> Step {
    match op {
        // -- session ---------------------------------------------------------
        "unmount" => {
            a.allow(&[]);
            ops::unmount(db, mount).map(|()| unit())
        }
        "renew_mount" => {
            a.allow(&["ttl_ms"]);
            ops::renew_mount(db, mount, a.opt_int("ttl_ms")).map(json)
        }
        "mount_info" => {
            a.allow(&[]);
            ops::mount_info(db, mount).map(json)
        }
        // -- structural ------------------------------------------------------
        "create_container" => {
            a.allow(&["path"]);
            ops::create_container(db, mount, a.str("path")).map(json)
        }
        "create_entry" => {
            a.allow(&["path", "data"]);
            ops::create_entry(db, mount, a.str("path"), a.opt_bytes("data").as_deref()).map(json)
        }
        "write_all" => {
            a.allow(&["path", "data"]);
            ops::write_all(db, mount, a.str("path"), &a.bytes("data")).map(|()| unit())
        }
        "append" => {
            a.allow(&["path", "data"]);
            ops::append(db, mount, a.str("path"), &a.bytes("data")).map(json)
        }
        "truncate" => {
            a.allow(&["path", "size"]);
            ops::truncate(db, mount, a.str("path"), a.int("size") as u64).map(|()| unit())
        }
        "write_range" => {
            a.allow(&["path", "offset", "data"]);
            ops::write_range(
                db,
                mount,
                a.str("path"),
                a.int("offset") as u64,
                &a.bytes("data"),
            )
            .map(json)
        }
        "link" => {
            a.allow(&["src", "dst"]);
            ops::link(db, mount, a.str("src"), a.str("dst")).map(|()| unit())
        }
        "create_special" => {
            a.allow(&["path", "type", "data"]);
            let data = a.opt_bytes("data").unwrap_or_default();
            ops::create_special(db, mount, a.str("path"), a.node_type("type"), &data).map(json)
        }
        "set_owner" => {
            a.allow(&["path", "uid", "gid", "mode"]);
            ops::set_owner(
                db,
                mount,
                a.str("path"),
                a.opt_int("uid"),
                a.opt_int("gid"),
                a.opt_int("mode"),
            )
            .map(|()| unit())
        }
        "set_atime" => {
            a.allow(&["node", "ts_ns"]);
            ops::set_atime(db, mount, &a.node("node"), a.int("ts_ns")).map(|()| unit())
        }
        "set_mtime" => {
            a.allow(&["node", "ts_ns"]);
            ops::set_mtime(db, mount, &a.node("node"), a.int("ts_ns")).map(|()| unit())
        }
        "set_xattr" => {
            a.allow(&["path", "name", "value"]);
            ops::set_xattr(db, mount, a.str("path"), a.str("name"), &a.bytes("value"))
                .map(|()| unit())
        }
        "get_xattr" => {
            a.allow(&["path", "name"]);
            ops::get_xattr(db, mount, a.str("path"), a.str("name"))
                .map(|v| v.map_or_else(unit, Out::Bytes))
        }
        "list_xattrs" => {
            a.allow(&["path"]);
            ops::list_xattrs(db, mount, a.str("path")).map(json)
        }
        "remove_xattr" => {
            a.allow(&["path", "name"]);
            ops::remove_xattr(db, mount, a.str("path"), a.str("name")).map(json)
        }
        "set_metadata" => {
            a.allow(&["path", "metadata"]);
            ops::set_metadata(db, mount, a.str("path"), &a.map("metadata")).map(|()| unit())
        }
        "set_retention" => {
            a.allow(&["path", "keep"]);
            ops::set_retention(db, mount, a.str("path"), a.opt_int("keep")).map(|()| unit())
        }
        "move" => {
            a.allow(&["src", "dst"]);
            ops::move_(db, mount, a.str("src"), a.str("dst")).map(|()| unit())
        }
        "copy" => {
            a.allow(&["src", "dst"]);
            ops::copy(db, mount, a.str("src"), a.str("dst")).map(json)
        }
        "rename" => {
            a.allow(&["path", "name"]);
            ops::rename(db, mount, a.str("path"), a.str("name")).map(|()| unit())
        }
        "remove" => {
            a.allow(&["path"]);
            ops::remove(db, mount, a.str("path")).map(|()| unit())
        }
        "remove_recursive" => {
            a.allow(&["path"]);
            ops::remove_recursive(db, mount, a.str("path")).map(|()| unit())
        }
        "pack" => {
            a.allow(&["path"]);
            ops::pack(db, mount, a.str("path")).map(json)
        }
        "unpack" => {
            a.allow(&["path"]);
            ops::unpack(db, mount, a.str("path")).map(|()| unit())
        }
        // -- read ------------------------------------------------------------
        "path_of" => {
            a.allow(&["node"]);
            ops::path_of(db, mount, &a.node("node")).map(json)
        }
        "stat" => {
            a.allow(&["path"]);
            ops::stat(db, mount, a.str("path")).map(json)
        }
        "stat_by_id" => {
            a.allow(&["node"]);
            ops::stat_by_id(db, mount, &a.node("node")).map(json)
        }
        "exists" => {
            a.allow(&["path"]);
            ops::exists(db, mount, a.str("path")).map(json)
        }
        "list" => {
            a.allow(&["path"]);
            ops::list(db, mount, a.opt_str("path").unwrap_or("/")).map(json)
        }
        "read_all" => {
            a.allow(&["path"]);
            ops::read_all(db, mount, a.str("path")).map(Out::Bytes)
        }
        // -- locking ---------------------------------------------------------
        "lock" => {
            a.allow(&["path", "ttl_ms"]);
            ops::lock(db, mount, a.str("path"), a.opt_int("ttl_ms")).map(json)
        }
        "unlock" => {
            a.allow(&["lock"]);
            ops::unlock(db, mount, &a.lock("lock")).map(|()| unit())
        }
        "renew_lock" => {
            a.allow(&["lock", "ttl_ms"]);
            ops::renew_lock(db, mount, &a.lock("lock"), a.opt_int("ttl_ms")).map(json)
        }
        // -- streaming -------------------------------------------------------
        "open_read" => {
            a.allow(&["path"]);
            ops::open_read(db, mount, a.str("path")).map(|desc| Out::Fd {
                desc,
                via: via.to_owned(),
            })
        }
        "open_write" => {
            a.allow(&["path", "mode", "lock"]);
            ops::open_write(
                db,
                mount,
                a.str("path"),
                a.write_mode("mode"),
                a.opt_lock("lock").as_ref(),
            )
            .map(|desc| Out::Fd {
                desc,
                via: via.to_owned(),
            })
        }
        // -- maintenance -----------------------------------------------------
        "verify" => {
            a.allow(&["deep"]);
            ops::verify(db, mount, a.opt_bool("deep").unwrap_or(false)).map(json)
        }
        other => panic!("{}: no operation {other:?} in this runner", a.at),
    }
}

// ---------------------------------------------------------------------------
// depth: inspections
// ---------------------------------------------------------------------------

/// Storage inspections — NOT Mount API operations. Dedup is deliberately
/// invisible through the API, so the one property that distinguishes
/// convergent from random mode cannot be asserted any other way.
fn inspect(op: &str, args: &Mapping, rig: &Rig, at: &str) {
    match op {
        "assert_pool_rows" => {
            let want = args["count"].as_i64().expect("count");
            let got: i64 = rig
                .primary()
                .connection()
                .query_row("SELECT count(*) FROM content_chunk", [], |r| r.get(0))
                .expect("count pool rows");
            assert_eq!(got, want, "{at}: expected {want} pool row(s), found {got}");
        }
        other => panic!("{at}: unimplemented inspection {other:?}"),
    }
}

// ---------------------------------------------------------------------------
// depth: matching
// ---------------------------------------------------------------------------

/// `expect` is a SUBSET match on records; lists of records are sorted by
/// (name, hidden-last) on both sides so no scenario depends on row order;
/// bytes match exactly; scalars compare by value (enums by their token).
fn check(expected: &Yaml, actual: &Out, binds: &HashMap<String, Out>, at: &str) {
    if is_bytes_literal(expected) {
        let want = decode(expected);
        match actual {
            Out::Bytes(got) => assert!(
                *got == want,
                "{at}: bytes differ\n  expected {}\n  got      {}",
                show(&want),
                show(got)
            ),
            other => panic!("{at}: expected bytes, got {}", describe(other)),
        }
        return;
    }
    if is_ref(expected) {
        match (deref(expected, binds), actual) {
            (Deref::Whole(Out::Json(want)), Out::Json(got)) => {
                assert!(want == got, "{at}: expected {want}, got {got}");
            }
            (Deref::Field(want), Out::Json(got)) => {
                assert!(&want == got, "{at}: expected {want}, got {got}");
            }
            (Deref::Whole(Out::Bytes(want)), Out::Bytes(got)) => {
                assert!(want == got, "{at}: bytes differ from the bound value");
            }
            (_, other) => panic!("{at}: cannot compare a ref with {}", describe(other)),
        }
        return;
    }
    let got = match actual {
        Out::Json(j) => j,
        other => panic!("{at}: expected {expected:?}, got {}", describe(other)),
    };
    match expected {
        Yaml::Sequence(items) => {
            let Json::Array(got_items) = got else {
                panic!("{at}: expected a list, got {got}");
            };
            let mut want: Vec<&Yaml> = items.iter().collect();
            let mut have: Vec<&Json> = got_items.iter().collect();
            if !want.is_empty() && want.iter().all(|i| i.is_mapping()) {
                want.sort_by_key(|i| yaml_sort_key(i));
                have.sort_by_key(|i| json_sort_key(i));
            }
            assert_eq!(
                want.len(),
                have.len(),
                "{at}: expected {} item(s), got {}",
                want.len(),
                have.len()
            );
            for (i, (w, h)) in want.iter().zip(have.iter()).enumerate() {
                check(w, &Out::Json((*h).clone()), binds, &format!("{at}[{i}]"));
            }
        }
        Yaml::Mapping(fields) => {
            let Json::Object(obj) = got else {
                panic!("{at}: expected a record, got {got}");
            };
            for (k, sub) in fields {
                let key = k.as_str().expect("field names are strings");
                let field = obj.get(key).unwrap_or_else(|| {
                    panic!(
                        "{at}: no field {key:?} in {:?}",
                        obj.keys().collect::<Vec<_>>()
                    )
                });
                check(
                    sub,
                    &Out::Json(field.clone()),
                    binds,
                    &format!("{at}.{key}"),
                );
            }
        }
        scalar => {
            let want = serde_json::to_value(scalar).expect("yaml scalar");
            assert!(&want == got, "{at}: expected {want}, got {got}");
        }
    }
}

/// (name, hidden-last) — never the engine's natural row order.
fn yaml_sort_key(v: &Yaml) -> (String, bool) {
    (
        v.get("name").map(scalar_text).unwrap_or_default(),
        !v.get("visible").and_then(Yaml::as_bool).unwrap_or(true),
    )
}

fn json_sort_key(v: &Json) -> (String, bool) {
    (
        v.get("name")
            .and_then(Json::as_str)
            .unwrap_or_default()
            .to_owned(),
        !v.get("visible").and_then(Json::as_bool).unwrap_or(true),
    )
}

fn describe(out: &Out) -> String {
    match out {
        Out::Json(j) => j.to_string(),
        Out::Bytes(b) => format!("bytes {}", show(b)),
        Out::Fd { desc, .. } => format!("descriptor {}", desc.fd),
    }
}

fn show(bytes: &[u8]) -> String {
    match std::str::from_utf8(bytes) {
        Ok(s) if bytes.len() <= 80 => format!("{s:?}"),
        _ => format!(
            "<{} bytes> {}",
            bytes.len(),
            hex::encode(&bytes[..bytes.len().min(32)])
        ),
    }
}
