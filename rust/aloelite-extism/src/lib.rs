//! Aloelite as an Extism plug-in.
//!
//! The deployment shape: the engine runs inside the host's WebAssembly
//! sandbox, the volume is a file on a host path the manifest granted, and
//! the host calls the Mount API by name over MessagePack. What that buys is
//! not speed — it costs a small multiple of native, and `README.md` says
//! what is known about how much — but reach and containment: any language
//! with an Extism SDK gets the engine without a binding written for it, and
//! the engine reaches exactly the paths the manifest names and nothing
//! else.
//!
//! | module | what it is |
//! |---|---|
//! | `exports` | the seven `#[plugin_fn]`s — `fs_open`, `fs_open_memory`, `fs_open_image`, `fs_snapshot`, `fs_call`, `fs_close`, `fs_operations` — compiled for WebAssembly only |
//! | [`plugin`] | the same work without the envelope, and the one handle behind it |
//! | [`wire`] | the envelope: `{op, args}` in, `{ok}` or `{error: {code, message}}` out |
//! | [`value`] | records, bytes and scalars on the way out, as MessagePack |
//! | [`args`] | how a MessagePack value answers what an argument is asked |
//! | [`platform`] | the clock and the CSPRNG, from WASI |
//!
//! Which operations exist and what each takes is `aloelite-api`, shared with
//! the browser surface: this crate decides only how a value is spelled.
//!
//! ## What a host must do
//!
//! - **Enable WASI.** Grant the directory the volume lives in if the host
//!   wants a file (`fs_open`); grant nothing at all if it would rather keep
//!   the bytes itself (`fs_open_image` / `fs_snapshot`), which leaves the
//!   plug-in able to read nothing it was not handed.
//! - **Allow at least 96 MiB of plug-in memory.** Argon2id at the format's
//!   pinned factors (64 MiB, t=3, p=4; ENC-2) runs inside the sandbox once
//!   per `create_volume` and once per `mount`. A 64 MiB cap is not enough
//!   and fails as `oom` rather than as anything about a key.
//! - **Instantiate once per volume**, and keep the instance: the handle,
//!   its mounts and its streaming descriptors live in that instance's
//!   memory.
//! - **Read the reply, not the error channel.** Every call succeeds at the
//!   Extism level; `{error: {code, message}}` is how an operation fails,
//!   with `code` from the spec's closed set (or one of
//!   [`value::EXTRA_CODES`]).
//!
//! `rust/aloelite-extism/README.md` has a worked example.

pub mod args;
pub mod platform;
pub mod plugin;
pub mod value;
pub mod wire;

// The Extism ABI itself, where that ABI exists. See the module's own note
// for why a native build must not carry it.
#[cfg(target_family = "wasm")]
pub mod exports;

pub use args::Msg;
pub use value::MsgpackSurface;
