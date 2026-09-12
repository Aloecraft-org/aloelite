# aloelite-extism

Aloelite as an [Extism](https://extism.org) plug-in: the Mount API over
MessagePack, in a WebAssembly sandbox, callable from any language with an
Extism SDK. The volume is either a file on a host path the manifest grants,
or bytes the host keeps and hands over — in which case the manifest grants
nothing at all.

| export | input | what it does |
|---|---|---|
| `fs_open` | `{path}` | open the volume file at `path` |
| `fs_open_memory` | — | a volume store in memory; nothing outlives the instance |
| `fs_open_image` | `{image}` | a volume store in memory, loaded from bytes the host kept |
| `fs_snapshot` | — | the whole database as bytes, for the host to keep |
| `fs_call` | `{op, args}` | run one Mount API operation by its spec name |
| `fs_close` | — | abort open descriptors and close the engine; idempotent |
| `fs_operations` | — | every name `fs_call` accepts |

Every export answers `{ok: …}` or `{error: {code, message}}`, and **none of
them fails at the Extism level** — read the reply, not the error channel.
`code` is the spec's error name (`not_found`, `bad_key`, `mount_conflict`, …)
or one of `usage`, `internal`, `sqlite`.

## Build

```sh
export WASI_SDK_PATH=/opt/wasi-sdk          # ../README.md on getting one
export CC_wasm32_wasip1=$WASI_SDK_PATH/bin/clang \
       AR_wasm32_wasip1=$WASI_SDK_PATH/bin/llvm-ar \
       CFLAGS_wasm32_wasip1=--sysroot=$WASI_SDK_PATH/share/wasi-sysroot
cargo build -p aloelite-extism --target wasm32-wasip1 --release
# target/wasm32-wasip1/release/aloelite_extism.wasm, about 2.8 MB
```

Preview **1**, not 2: Extism's runtime instantiates core modules over
`wasi_common`, wasmtime's preview-1 implementation, and cannot load a
component. The CLI's `wasm32-wasip2` build is the other WASI target and a
different artifact.

## Call it

```python
from extism import Plugin, Manifest
import msgpack

manifest = Manifest(
    wasm=[{"path": "aloelite_extism.wasm"}],
    allowed_paths={"/srv/volumes": "/vol"},
    memory={"max_pages": 2048},            # 128 MiB; see below
)

def call(plugin, export, value=None):
    reply = msgpack.unpackb(plugin.call(export, msgpack.packb(value, use_bin_type=True)))
    if "error" in reply:
        raise RuntimeError(f"{reply['error']['code']}: {reply['error']['message']}")
    return reply["ok"]

with Plugin(manifest, wasi=True) as p:
    call(p, "fs_open", {"path": "/vol/notes.fs"})
    vol = call(p, "fs_call", {"op": "create_volume", "args": {"name": "notes", "pin": b"hunter2"}})
    m = call(p, "fs_call", {"op": "mount", "args": {"volume": vol["id"], "pin": b"hunter2"}})
    call(p, "fs_call", {"op": "create_entry",
                        "args": {"mount": m, "path": "/hello.txt", "data": b"one file"}})
    print(call(p, "fs_call", {"op": "read_all", "args": {"mount": m, "path": "/hello.txt"}}))
    call(p, "fs_call", {"op": "unmount", "args": {"mount": m}})
    call(p, "fs_close")
```

## Two storage shapes

**A file**, through WASI, on a granted path. Durability per transaction, and
the volume can be larger than plug-in memory. What the example above uses.

**A memory image**, where the host keeps the bytes:

```python
with Plugin(Manifest(wasm=[{"path": "aloelite_extism.wasm"}],
                     memory={"max_pages": 2048}), wasi=True) as p:
    call(p, "fs_open_image", {"image": load_from_wherever() or b""})
    ...
    store_wherever(call(p, "fs_snapshot"))
```

Note what that manifest does not say: there are no `allowed_paths`, so the
plug-in can read nothing it was not handed. The volume can live in S3, a
key/value store, a Postgres column, an encrypted field the host already has
— Aloelite's premise is that a filesystem is one file, and this is that file
as a value. An empty image is a fresh volume store.

The costs are real and are the host's to manage: the volume must fit in
plug-in memory, a snapshot is the whole database, and **durability is per
snapshot** — an unsnapshotted write is a lost write. Nothing here decides
when to take one; that policy is the host's (D-7), the same as it is for
`aloelite_store::image::Image`, which is the same shape in the crate that
owns storage models.

## What a host must get right

- **Enable WASI.** Grant the directory if you want `fs_open`; grant nothing
  if you would rather hand over bytes. `fs_open` on an ungranted path fails
  like a missing file.
- **Allow at least 96 MiB of plug-in memory** (1536 pages; 2048 is
  comfortable). Argon2id at the format's pinned factors — 64 MiB, t=3, p=4,
  `conformance/vectors/format-v1.json` — runs *inside* the sandbox once per
  `create_volume` and once per `mount`. A 64 MiB cap fails as `oom`, which
  reads like a bug in the plug-in and is not one.
- **One instance per volume, kept alive.** The handle, its mounts and its
  streaming descriptors live in that instance's linear memory.
- **Unmount before you close.** A mount row is durable (D-4): an rw mount
  left behind meets the next instance as `mount_conflict`, not as whatever
  it was actually trying to do.

## Why MessagePack

The Mount API's values are 64-bit integers and byte strings. JSON has
neither: bytes would travel base64'd, and a nanosecond timestamp — `2^60`ish,
where a double's spacing is 256 ns — would come back rounded by any host that
parses into a double, which is most of them. This is the same problem the
browser surface solves with `BigInt`. MessagePack has `int64` and `bin`, so
the spec's types survive the trip; the writer is `rmp-serde`'s `to_vec_named`,
byte-identical to Python's `msgpack.packb(use_bin_type=True)`.

A host that wants JSON converts on its own side, where it can see the
precision question rather than meeting it in a timestamp two months later.

## Why the exports are prefixed

A wasm export is a symbol, and it competes with wasi-libc's. Exporting
`open` makes the linker refuse the module outright — the harmless case.
Exporting `close` is the other one: the linker takes the plug-in's `close`
and never pulls libc's, SQLite reaches the platform through a table of
function pointers, and the first directory sync calls `osClose(fd)` through
a function that takes no arguments. That surfaces as
`wasm trap: indirect call type mismatch`, four frames inside SQLite, nowhere
near the export that caused it.

`harness/` is what caught it, and is why it exists: a small Extism host that
loads the built `.wasm`, drives a real workload through it and asserts. It is
a separate workspace because the host SDK brings wasmtime, and nobody running
`cargo test` on the engine should have to build a WebAssembly runtime.

```sh
cargo run --release --manifest-path aloelite-extism/harness/Cargo.toml -- \
  target/wasm32-wasip1/release/aloelite_extism.wasm "$(mktemp -d)"
```

## What it costs

Two facts are properties of the shape and hold wherever it runs. Argon2id
runs inside the sandbox, so `create_volume` and `mount` cost what wasm costs
to do 64 MiB of hashing. And SQLite falls back from WAL to a rollback
journal, because WAL needs shared memory WASI has no way to offer — so write
durability here is a journal, not a log.

For the rest, `extism` is a frontend in the benchmark harness: the
`throughput`, `smallfile`, `random` and `append` suites report it beside
`direct` on the same corpora with the same barrier, so the gap between those
two rows is what the sandbox costs and nothing else.
`doc/BENCHMARKS.md` has the method, and the tables there are what to read
rather than anything quoted here. The shape of it, from the runs behind that
integration: roughly 2x on the Argon2id that dominates `create_volume` and
`mount`, 2x on small operations, and 3-5x on bulk reads and writes, with the
Extism call boundary itself a minority of the gap — most of it is the engine
running in WebAssembly.
