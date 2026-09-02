# aloelite-rs

The Rust implementation of the Aloelite Mount API. One workspace, six crates,
three targets. `doc/RUST_PORT.md` is the plan; `doc/DECISIONS.md` D-7 is the
storage decision this layout follows from.

```
aloelite-core/          the engine. compiles to every target with zero cfg
aloelite-store/         how a connection is opened: file / memory+blob / OPFS
aloelite-conformance/   conformance/ scenarios + vectors, under cargo test and
                        wasm-bindgen-test alike
aloelite-fuse/          Linux FUSE daemon (fuser). native only
aloelite-wasm/          browser surface. Dedicated Worker, volume in OPFS
aloelite-cli/           the aloelite command. native + wasm32-wasip2
```

| target | crates |
|---|---|
| native | all six |
| `wasm32-wasip2` | core, store, conformance, cli |
| `wasm32-unknown-unknown` | core, store, conformance, wasm |

## The one rule

`aloelite-core` compiles to all three targets with zero `cfg`, performs no
I/O of its own, and never asks which platform it is on. CI checks it on every
push (`.github/workflows/main.yml`, job `rust`). Anything that cannot meet
that bar is a different crate.

## Build

```sh
cargo check                                                    # native, everything
cargo check -p aloelite-core -p aloelite-store \
  --target wasm32-unknown-unknown                              # the rule, checked
cargo check -p aloelite-core -p aloelite-store --target wasm32-wasip2
cargo test                                                     # native
cargo test -p aloelite-conformance -p aloelite-store -p aloelite-wasm \
  --target wasm32-unknown-unknown                              # in Firefox, headless
cargo build -p aloelite-wasm --target wasm32-unknown-unknown --release
wasm-bindgen --target web --typescript --out-dir pkg \
  target/wasm32-unknown-unknown/release/aloelite_wasm.wasm      # the ES module a page imports
```

The wasm test run needs `wasm-bindgen-test-runner` at **exactly** the
`wasm-bindgen` version in `Cargo.lock` — the bindgen schema is unstable and
the runner refuses a mismatch. The devcontainer pins it; CI reads it from the
lockfile.

Without Firefox, Chromium works too. The runner treats any stderr from the
WebDriver binary as a failed start, and chromedriver prints an IPv6 warning
in containers without IPv6, so silence it:

```sh
CHROMEDRIVER=/path/to/chromedriver CHROMEDRIVER_ARGS=--silent \
  cargo test -p aloelite-conformance -p aloelite-store -p aloelite-wasm \
    --target wasm32-unknown-unknown
```

A `webdriver.json` next to the crate can point at a Chrome binary
(`{"goog:chromeOptions": {"binary": "..."}}`); the runner adds `headless`
and `no-sandbox` itself.

## Status

`aloelite-core` implements the whole Mount API and passes the conformance
suite natively and in a browser: 94 scenarios, every harness, every vector.
`aloelite-store` opens a connection three ways — a file, a memory image
checkpointed to a `BlobStore` blob, and the browser's OPFS pool — and each
is tested where it runs. `aloelite-wasm` is the browser surface over them:
`Fs.call(op, args)`, the Worker protocol, and the OPFS pool with its Web
Lock; `aloelite-wasm/README.md` has the page-side snippet. The fuse and cli
crates are next. `doc/RUST_PORT.md` "Standing" has the detail.
