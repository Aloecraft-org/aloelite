# aloelite-cli

The `aloelite` command over `aloelite-core`, to the contract in
`aloelite/config/cli.yaml` — the same verbs, flags, output and exit codes as
the Python `aloelite`, asserted from both sides (`tests/contract.rs` here,
`tests/test_cli_contract.py` there). Session per invocation: open, mount,
operate, unmount, close.

```sh
aloelite -f notebook.fs                     # creates the file (with a default volume) or shows what's inside
aloelite -f notebook.fs put report.pdf /docs/report.pdf
aloelite -f notebook.fs ls -l /docs
aloelite -f notebook.fs get /docs/report.pdf -           # to stdout
aloelite -f notebook.fs put -r ./project /code           # a whole tree in
aloelite -f notebook.fs get -r /code ./restored          # a whole tree out
aloelite -f notebook.fs -v vault --pin-env VAULT_PIN ls  # an encrypted volume
aloelite --help                                          # every verb
```

`-v` is name-first, id-fallback (canonical uuid7 or bare hex); with no `-v` a
file holding exactly one volume uses it. The PIN comes from `--pin SECRET`,
`--pin-file`, or `--pin-env`; a bare `--pin` prompts on the terminal with
echo off (`aloelite --pin -f FILE ls`).

## As a WASI component

```sh
cargo build -p aloelite-cli --target wasm32-wasip2 --release
wasmtime run --dir=.::/work target/wasm32-wasip2/release/aloelite.wasm -f /work/notebook.fs ls /
```

The volume lives on a host directory the runtime preopens (`--dir`); paths
inside the component are the guest side of that mapping. There is no
terminal to prompt on: a bare `--pin` says so and exits 1, so use
`--pin-file` or `--pin-env`. The `wasm32-wasip2` build needs a WASI-capable
clang for SQLite (`../README.md`).

## Known differences from the Python command

Recorded in the contract's `known_differences` so nobody hunts a bug that is
a decision: `volumes` and `mounts` print the UTC minute where Python prints
the local one; `stat` prints metadata keys sorted; `aloelite fuse|web|admin`
name the right program instead of delegating (FUSE is the `aloelite-fuse`
binary here; web and admin are Python-only).

## Tests

```sh
cargo test -p aloelite-cli   # unit tests, the contract projection, and tests/test_cli.py ported through the binary
```
