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
cargo check                                                        # native, everything
cargo check -p aloelite-core --target wasm32-unknown-unknown       # the rule, checked
cargo check -p aloelite-core --target wasm32-wasip2
cargo test                                                         # native
cargo test -p aloelite-conformance --target wasm32-unknown-unknown # in Firefox, headless
```

The wasm test run needs `wasm-bindgen-test-runner` at **exactly** the
`wasm-bindgen` version in `Cargo.lock` — the bindgen schema is unstable and
the runner refuses a mismatch. The devcontainer pins it; CI reads it from the
lockfile.

## Status

Scaffold. Manifests, READMEs and the CI guard exist; no engine code does.
