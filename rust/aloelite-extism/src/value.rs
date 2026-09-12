//! What crosses the boundary outward: MessagePack, because the Mount API's
//! value model is MessagePack's.
//!
//! The browser surface had to choose how to spell an integer and settled on
//! `BigInt`, because a nanosecond timestamp sits near 2^60 where a double's
//! spacing is 256 ns and a `set_mtime` round-tripped through one comes back
//! changed. A plug-in wire faces the same question, and JSON answers it
//! badly twice over: it has no integer type distinct from a double, and no
//! binary type at all, so bytes would travel base64'd (a third larger, and
//! decoded by hand at both ends) and timestamps would silently lose their
//! low bits in any host that parses into a double -- which is most of them.
//!
//! MessagePack has both: `int64`/`uint64` exactly, and `bin` for bytes. So
//! the wire is MessagePack, every Extism SDK's language has a library for
//! it, and the spec's types survive the trip. A host that prefers JSON
//! converts on its own side, where it can see the precision question rather
//! than discovering it in a timestamp two months later.
//!
//! The writer is `rmp-serde`'s `to_vec_named`: the same codec the pack
//! format uses, byte-identical to Python's
//! `msgpack.packb(use_bin_type=True)`, and named rather than positional so a
//! record arrives as a map with the spec's field names rather than a tuple
//! the host has to know the order of.
//!
//! An outbound value is therefore its own encoding — `Out = Vec<u8>`, one
//! MessagePack value — and the envelope in [`crate::wire`] concatenates
//! rather than decoding and re-encoding. For `read_all` of a large entry
//! that is the difference between two copies of the payload and four.

use aloelite_api::Surface;
use serde::Serialize;

use crate::args::Msg;

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// Error codes this plug-in can raise that are NOT in the spec's closed set.
///
/// Exactly the dispatch's three, and nothing of its own: everything that can
/// go wrong here that is not an engine error is a malformed request
/// (`usage`) or SQLite refusing to open the file the manifest granted
/// (`sqlite`). There is no admission lock to be `busy` on and no blob store
/// to fail -- the host's manifest decides what this plug-in can reach, and
/// it decides it before the plug-in runs.
pub use aloelite_api::EXTRA_CODES;

/// MessagePack's `nil`, the one-byte encoding.
pub const NIL: u8 = 0xc0;

/// The plug-in's pair of value types: a MessagePack value in, one
/// MessagePack value's encoding out.
pub struct MsgpackSurface;

impl Surface for MsgpackSurface {
    type In = Msg;
    type Out = Vec<u8>;

    /// A record or scalar: maps for records and string maps, arrays for
    /// lists, integers for integers, `nil` for an absent optional.
    fn record<T: Serialize + ?Sized>(value: &T) -> Vec<u8> {
        rmp_serde::to_vec_named(value).expect("records serialize")
    }

    /// Bytes, as MessagePack's `bin` -- not a string, and not an array of
    /// numbers. Written straight into the buffer that carries them.
    fn bytes(b: &[u8]) -> Vec<u8> {
        let mut out = Vec::with_capacity(b.len() + 5);
        rmp::encode::write_bin(&mut out, b).expect("a Vec never fails to write");
        out
    }

    /// A `void` result. MessagePack draws no undefined/null distinction, so
    /// this and [`MsgpackSurface::null`] are the same value; the browser's
    /// `undefined` has no counterpart here.
    fn unit() -> Vec<u8> {
        vec![NIL]
    }

    fn null() -> Vec<u8> {
        vec![NIL]
    }
}
