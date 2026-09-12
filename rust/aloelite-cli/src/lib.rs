//! The `aloelite` command.
//!
//! A port of `aloelite/cli.py` to the contract both now share,
//! `aloelite/config/cli.yaml`: the same verbs, flags, positionals and exit
//! codes, asserted from both sides (`tests/contract.rs` here,
//! `tests/test_cli_contract.py` there), so the command a script relies on
//! cannot drift between the two implementations. Where the two legitimately
//! read differently the contract's `known_differences` says so.
//!
//! Session per invocation: open the file, resolve the volume, mount, run
//! the verb, unmount, close. Nothing here knows anything the Mount API does
//! not say, with two exceptions the reference makes too: the volume listing
//! reads each volume's encryption mode straight from its row, and `prune
//! --vacuum` runs `VACUUM` on the connection.
//!
//! Builds for `wasm32-wasip2` as well as native: a WASI component under
//! wasmtime, the volume on the host filesystem (`--dir`). No FUSE, no
//! kernel, and no terminal to prompt on — a bare `--pin` there says so.
//!
//! | module | what |
//! |---|---|
//! | [`args`] | the verb and global tables — the contract's Rust twin — and the parser over them |
//! | [`verbs`] | one function per verb, the no-verb status/create, and the dispatch |
//! | [`transfer`] | `put -r` / `get -r`: a walk over single-node operations, cp -r's destination rule |
//! | [`volume`] | volume selection: name first, id (dashed or bare hex) second, refuse to guess |
//! | [`pin`] | the three PIN sources and the terminal prompt |
//! | [`fail`] | one error type, and the reference's message mapping at the top level |
//! | [`text`] | the minute-resolution timestamp, the pluraliser, Python's dict syntax |

pub mod args;
pub mod fail;
pub mod pin;
pub mod text;
pub mod transfer;
pub mod verbs;
pub mod volume;

/// The process entry: parse `std::env::args`, run, return the exit code.
pub fn main() -> i32 {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    verbs::run(&argv)
}
