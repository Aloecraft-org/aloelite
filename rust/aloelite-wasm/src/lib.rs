//! Aloelite in the browser.
//!
//! The deployment shape (doc/DECISIONS.md D-7): the engine runs inside a
//! **Dedicated Worker**, the volume file lives in **OPFS** through
//! `sqlite-wasm-vfs`'s `sahpool` VFS, and the page talks to it across
//! `postMessage`. This crate is that surface, in three layers a host can
//! enter at any level:
//!
//! | export | what it is | where it runs |
//! |---|---|---|
//! | [`Fs`] | the engine behind one handle: `call(op, args)` runs any Mount API operation by its spec name, with the spec's parameter names ([`fs::OPS`] is the table) | anywhere the wasm loads; `Fs.openMemory()` needs no storage at all |
//! | [`serve`] | the message protocol: `{id, op, args}` in, `{id, ok}` or `{id, error: {code, message}}` out, over the Worker's own `self` or one end of a `MessageChannel` | a Worker, usually |
//! | `Pool` | the OPFS pool: `install` once per Worker, `open(name)` a volume file under the Web Lock that makes it single-writer, plus export / import / delete | a Dedicated Worker only |
//!
//! Three consequences of the shape that are features, not limitations:
//!
//! - The engine stays synchronous. OPFS sync access handles exist only in
//!   Workers, so putting the engine there is what lets it be the *same*
//!   engine as native — the conformance scenarios run unchanged.
//! - Argon2id (64 MiB / 3 / 4, pinned in format-v1.json) blocks the Worker
//!   for well under a second once per mount, and blocks nothing the user
//!   can see.
//! - One Worker owns one volume. Two tabs are two mounts (ACC-1), and the
//!   browser target is single-writer per volume by a Web Lock — D-4's
//!   admission policy, enforced by the platform. `Pool.open` takes that
//!   lock and answers `busy` instead of waiting when another Worker has it.
//!
//! What crosses the boundary, in both directions, is decided once in
//! [`value`] and [`args`]: every integer is a `BigInt` (timestamps are
//! nanoseconds and do not fit a double; `Number(x)` where a number is
//! wanted), bytes are `Uint8Array` (a string is accepted as UTF-8 on the way
//! in), an absent optional is `null`, and an error is an `Error` whose
//! `code` is the spec's error name.

pub mod args;
pub mod fs;
pub mod serve;
pub mod value;
pub mod weblock;

#[cfg(all(target_arch = "wasm32", target_os = "unknown"))]
pub mod pool;

pub use fs::Fs;
pub use serve::{Server, serve};
