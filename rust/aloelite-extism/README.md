# aloelite-extism

Aloelite as an [Extism](https://extism.org) plug-in: the Mount API over
MessagePack, in a WebAssembly sandbox, callable from any language with an
Extism SDK. The volume is a file on a host path the manifest grants.

| export | input | what it does |
|---|---|---|
| `fs_open` | `{path}` | open the volume file at `path` |
| `fs_open_memory` | — | a volume store in memory; nothing outlives the instance |
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

## What a host must get right

- **Enable WASI and grant the directory.** The plug-in reaches the host
  filesystem only through `allowed_paths`; `fs_open` on anything else fails
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

For the rest: a prototype of this plug-in, driven on one host against the
same engine natively, came out around 2x on Argon2id-bound operations, 2x on
small ones and 4-5x on bulk reads and writes, with the Extism boundary itself
accounting for well under a quarter of the gap — the rest is the engine
running in WebAssembly. Those are indicative single-run numbers, not
`bench/` output: an Extism backend for the benchmark suite is the follow-up
that would put them beside the frontends in `doc/BENCHMARKS.md` on equal
terms.
