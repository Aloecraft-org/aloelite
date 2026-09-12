//! The Extism ABI: the seven `#[plugin_fn]` exports, and nothing else.
//!
//! Compiled only for WebAssembly. A `#[plugin_fn]` is a `#[no_mangle]`
//! function that reaches for `input_load_u8`, `alloc` and `output_set` —
//! symbols the Extism host provides and nothing else does — so an rlib
//! carrying one will not link into a native test binary. Keeping them in
//! their own module is what lets the rest of the crate, and its tests, be
//! ordinary Rust. CI type-checks this module by building the crate for
//! `wasm32-wasip1`, and `tests/harness/` runs it.
//!
//! Each export is one line of its own: decode, do the work, answer the
//! envelope. The work is [`crate::plugin`].
//!
//! **Every export is prefixed `fs_`, and that is not decoration.** A wasm
//! export is a symbol, and it competes with wasi-libc's: exporting `open`
//! makes the linker refuse the module outright (a duplicate of libc's
//! `open`), which is the harmless case. Exporting `close` is the other one
//! — the linker takes this module's `close` and never pulls libc's, SQLite
//! reaches the platform through a table of function pointers, and the first
//! directory sync calls `osClose(fd)` through a function that takes no
//! arguments. What that produces is not a link error but
//! `wasm trap: indirect call type mismatch`, four frames inside SQLite,
//! nowhere near the export that caused it. `tests/plugin.rs` cannot see
//! this; only `harness/` can, which is what it is for.

use aloelite_api::{Handle, Surface};
use extism_pdk::*;

use crate::plugin;
use crate::value::MsgpackSurface;
use crate::wire;

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// The arguments `fs_open` takes.
pub const OPEN_ARGS: &[&str] = &["path"];

/// The arguments `fs_open_image` takes.
pub const OPEN_IMAGE_ARGS: &[&str] = &["image"];

#[plugin_fn]
pub fn fs_open(input: Vec<u8>) -> FnResult<Vec<u8>> {
    Ok(wire::reply(
        wire::arguments("fs_open", &input)
            .and_then(|a| {
                a.allow(OPEN_ARGS)?;
                plugin::open_file(&a.str("path")?)
            })
            .map(|()| MsgpackSurface::unit()),
    ))
}

#[plugin_fn]
pub fn fs_open_memory(_: ()) -> FnResult<Vec<u8>> {
    Ok(wire::reply(
        plugin::open_in_memory().map(|()| MsgpackSurface::unit()),
    ))
}

#[plugin_fn]
pub fn fs_open_image(input: Vec<u8>) -> FnResult<Vec<u8>> {
    Ok(wire::reply(
        wire::arguments("fs_open_image", &input)
            .and_then(|a| {
                a.allow(OPEN_IMAGE_ARGS)?;
                plugin::open_image(&a.opt_bytes("image")?.unwrap_or_default())
            })
            .map(|()| MsgpackSurface::unit()),
    ))
}

#[plugin_fn]
pub fn fs_snapshot(_: ()) -> FnResult<Vec<u8>> {
    Ok(wire::reply(
        plugin::snapshot().map(|image| MsgpackSurface::bytes(&image)),
    ))
}

#[plugin_fn]
pub fn fs_call(input: Vec<u8>) -> FnResult<Vec<u8>> {
    Ok(wire::reply(
        wire::decode(&input).and_then(|r| plugin::dispatch(&r.op, &r.args)),
    ))
}

#[plugin_fn]
pub fn fs_close(_: ()) -> FnResult<Vec<u8>> {
    Ok(wire::reply(plugin::shut().map(|()| MsgpackSurface::unit())))
}

#[plugin_fn]
pub fn fs_operations(_: ()) -> FnResult<Vec<u8>> {
    Ok(wire::reply(Ok(MsgpackSurface::record(
        &Handle::operations(),
    ))))
}
