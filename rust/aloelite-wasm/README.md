# aloelite-wasm

The browser surface over `aloelite-core`, meant to run inside a Dedicated
Worker with the volume file in OPFS (`doc/DECISIONS.md` D-7). Three exports,
and a page can enter at any level:

| export | what it is |
|---|---|
| `Fs` | the engine behind one handle: `fs.call(op, args)` runs any Mount API operation by its spec name |
| `serve(fs, endpoint)` | the Worker protocol: `{id, op, args}` in, `{id, ok}` or `{id, error: {code, message}}` out |
| `Pool` | the OPFS pool: `Pool.install()` once per Worker, `pool.open(name)` a volume file under the Web Lock that makes it single-writer |

## Build the package

```sh
cargo build -p aloelite-wasm --target wasm32-unknown-unknown --release
wasm-bindgen --target web --typescript --out-dir pkg \
  target/wasm32-unknown-unknown/release/aloelite_wasm.wasm
```

`wasm-bindgen` must be the version in `Cargo.lock` (`../README.md` says
how). The output is an ES module, `pkg/aloelite_wasm.js`, its `.wasm`, and
a `.d.ts`. CI generates the same and fails on a signature TypeScript cannot
type.

## The Worker

```js
// worker.js — module worker
import init, { Pool, serve } from "./pkg/aloelite_wasm.js";

await init();
const pool = await Pool.install();      // OPFS directory ".aloelite", VFS "aloelite-opfs", 6 files
const fs = await pool.open("main.db");  // throws {code: "busy"} if another Worker has it open
serve(fs, self);                        // from here on, messages are the only way in
```

`Pool.install` takes `{directory, vfsName, initialCapacity}`, all optional.
A database and its rollback journal take two of the pool's files;
`pool.addCapacity(n)` grows it. `pool.export(name)` returns the whole SQLite
file as a `Uint8Array`, `pool.import(name, bytes)` installs one,
`pool.delete(name)`, `pool.exists(name)`, `pool.list()` and `pool.capacity`
do what they say.

## The page

```js
const worker = new Worker("worker.js", { type: "module" });
let seq = 0;
const pending = new Map();
worker.onmessage = ({ data }) => {
  const { resolve, reject } = pending.get(data.id);
  pending.delete(data.id);
  if ("error" in data) {
    reject(Object.assign(new Error(data.error.message), { code: data.error.code }));
  } else {
    resolve(data.ok);
  }
};
const call = (op, args) =>
  new Promise((resolve, reject) => {
    const id = ++seq;
    pending.set(id, { resolve, reject });
    worker.postMessage({ id, op, args });
  });

const volume =
  (await call("resolve_volume_name", { name: "docs" })) ??
  (await call("create_volume", { name: "docs" })).id;
const mount = await call("mount", { volume });
await call("create_entry", { mount, path: "/hello.txt", data: "hello" });
const bytes = await call("read_all", { mount, path: "/hello.txt" }); // Uint8Array
const info = await call("stat", { mount, path: "/hello.txt" });      // info.size === 5n
await call("unmount", { mount });
```

## The protocol

A request is `{id, op, args}`. `id` is anything and comes back untouched.
`op` is an operation name from `aloelite/config/mount-api.yaml`
(`Fs.operations()` lists them at run time); `args` is an object keyed by
that operation's parameter names, minus `fs`, which is the handle. The
spec's `session.open` is not an operation here — it is `Pool.open` /
`Fs.openMemory` — and its `session.close` is `fs.close()` / `server.close()`;
the `close` on the wire is the streaming one, taking an `fd`. Both
handle closes return a promise that resolves once the volume's lock is
released. One operation is not in the spec and is documented as such:
`resolve_volume_name({name})` → the id of the most recently created volume
of that name, or `null`.

A reply is `{id, ok}` on success, with `ok` the operation's return (`void`
returns come back as `undefined`), or `{id, error: {code, message}}`.
Every request is answered, including one with no `op` (`code: "usage"`).

What crosses the boundary:

| the spec says | on the way in | on the way out |
|---|---|---|
| `int`, `Timestamp` | a `Number` that is a safe integer, or a `BigInt` — a `Number` beyond 2^53 is refused, never rounded | always a `BigInt` (timestamps are nanoseconds and do not fit a double; sizes and counts follow for consistency: `Number(x)` when you want a number) |
| `bytes` / `Bytes` | `Uint8Array`, `ArrayBuffer`, or a string (UTF-8) | `Uint8Array`, transferred rather than copied |
| ids, `Path`, enums | strings (`enc_mode`, `access`, `type`, `mode`, `whence` by their spec tokens) | strings |
| `{string: string}` (`metadata`) | a plain object | a plain object |
| an absent optional | leave it out, or `null` / `undefined` | `null` |
| records (`NodeInfo`, `VolumeInfo`, …) | — | plain objects with the spec's field names |
| `Descriptor` | — | `{fd, node, writable}`; pass `fd` to `read` / `write` / `seek` / `tell` / `close` / `abort` |

An unknown argument name is an error (`usage`), not ignored — a misspelled
optional would otherwise change behaviour in silence.

### Errors

`code` is one of the spec's error names (`not_found`, `mount_invalid`,
`lock_held`, `bad_key`, …, the closed set in `mount-api.yaml`) or one of
six this surface adds, disjoint from them by test:

| code | meaning |
|---|---|
| `usage` | the request was wrong: unknown operation or argument, wrong type, a closed handle, an unknown `fd` |
| `internal` | an engine invariant failed (a bug) |
| `sqlite` | SQLite refused something the engine did not anticipate |
| `busy` | the volume file is open in another Worker (`Pool.open`) |
| `opfs` | the pool refused: no capacity, storage denied |
| `io` | a blob store failed (not raised by this crate's own openers) |

Directly (`fs.call`), the thrown value is an `Error` named `AloeliteError`
with a `code` property. Over messages it is the plain `{code, message}`
object above, because structured clone drops an `Error`'s own properties.

## Direct use, and other endpoints

`serve` takes anything with `postMessage` and an `onmessage` slot, so one
end of a `MessageChannel` works as well as the Worker's `self`, and a
Worker can serve several pages. Without messages at all,
`Fs.openMemory()` gives an in-memory volume store — nothing persists past
the handle — for demos, tests, and a page that keeps its own bytes; the
conformance engine underneath is the same.

## Single-writer

Two Workers each holding an OPFS access handle on one file would corrupt
it, so `Pool.open(name)` first takes the exclusive Web Lock
`aloelite:<directory>/<name>` with `ifAvailable`, and fails with `busy`
rather than queueing when another context holds it. `await fs.close()` (or
`await server.close()`) releases the lock and resolves once the release
has happened — a browser releases asynchronously, so an `open` of the same
file in the same turn as an un-awaited close would still see `busy` — and
the browser releases it itself if the Worker dies. A page that wants to
wait for the volume does so explicitly with its own retry. Where
`navigator.locks` does not exist the open fails with `unsupported` rather
than opening unprotected.

## Tests

```sh
cargo test -p aloelite-wasm                                   # the dispatch table against the spec
cargo test -p aloelite-wasm --target wasm32-unknown-unknown   # the same, plus call, protocol and pool in a browser
```

The browser tests need `wasm-bindgen-test-runner` at the lockfile's
version and a WebDriver (`../README.md`); the pool's tests run in a
dedicated worker, because that is the only place OPFS access handles exist.
