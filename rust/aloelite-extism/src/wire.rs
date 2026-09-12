//! The envelope: `{op, args}` in, `{ok: …}` or `{error: {code, message}}`
//! out.
//!
//! The same shape `aloelite-wasm`'s `postMessage` protocol uses, minus the
//! `id` — an Extism call is one request and one reply, with nothing to
//! match up — and for the same reason it exists there. The browser puts the
//! error in the payload because structured clone drops an `Error`'s own
//! properties and the code would not survive the trip; Extism's error
//! channel is a bare string, so a code put through it would arrive glued to
//! the message for the host to parse back out. The spec's error is a code,
//! so the code travels as a field.
//!
//! The consequence a host must know: **a call succeeds at the Extism level
//! even when the operation failed.** The reply is always a map; which key it
//! carries says which happened.

use aloelite_api::{Args, code};
use aloelite_core::FsError;
use rmpv::Value as Mp;

use crate::args::Msg;

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// The keys a request map may carry. Anything else is refused rather than
/// ignored, for the reason `Args::allow` refuses an unknown argument: a
/// misspelled `args` would otherwise run the operation with none.
pub const REQUEST_KEYS: &[&str] = &["op", "args"];

/// One decoded request.
pub struct Request {
    pub op: String,
    pub args: Msg,
}

/// Read `{op, args}` out of a request body.
pub fn decode(input: &[u8]) -> Result<Request, FsError> {
    let value = rmpv::decode::read_value(&mut &input[..])
        .map_err(|e| FsError::usage(format!("request is not MessagePack: {e}")))?;
    let Mp::Map(pairs) = &value else {
        return Err(FsError::usage("request must be a map with string keys"));
    };
    let mut op = None;
    let mut args = Mp::Nil;
    for (k, v) in pairs {
        let Mp::String(k) = k else {
            return Err(FsError::usage("request must be a map with string keys"));
        };
        match k.as_str() {
            Some("op") => op = Some(v),
            Some("args") => args = v.clone(),
            other => {
                return Err(FsError::usage(format!(
                    "request: unknown key {:?}; it takes {REQUEST_KEYS:?}",
                    other.unwrap_or("<not UTF-8>")
                )));
            }
        }
    }
    let op = op.ok_or_else(|| FsError::usage("request has no `op`"))?;
    let Mp::String(op) = op else {
        return Err(FsError::usage("request: `op` must be a string"));
    };
    let op = op
        .as_str()
        .ok_or_else(|| FsError::usage("request: `op` must be a string"))?;
    Ok(Request {
        op: op.to_owned(),
        args: Msg(args),
    })
}

/// Read a bare arguments map — what the exports that are not `call` take,
/// since they are not operations and have no `op`. Goes through the same
/// `Args` as everything else, so `open` refuses an unknown key and names a
/// missing one in the same words the operations do.
pub fn arguments(op: &str, input: &[u8]) -> Result<Args<Msg>, FsError> {
    let value = if input.is_empty() {
        Mp::Nil
    } else {
        rmpv::decode::read_value(&mut &input[..])
            .map_err(|e| FsError::usage(format!("{op}: input is not MessagePack: {e}")))?
    };
    Args::read(op, &Msg(value))
}

/// Render a result as the reply body. `body` is already one MessagePack
/// value (that is what `MsgpackSurface::Out` is), so the envelope is written
/// around it rather than over it.
pub fn reply(result: Result<Vec<u8>, FsError>) -> Vec<u8> {
    let mut out = Vec::new();
    let w = &mut out;
    rmp::encode::write_map_len(w, 1).expect(WRITE);
    match result {
        Ok(body) => {
            rmp::encode::write_str(w, "ok").expect(WRITE);
            out.extend_from_slice(&body);
        }
        Err(e) => {
            rmp::encode::write_str(w, "error").expect(WRITE);
            rmp::encode::write_map_len(w, 2).expect(WRITE);
            rmp::encode::write_str(w, "code").expect(WRITE);
            rmp::encode::write_str(w, code(&e)).expect(WRITE);
            rmp::encode::write_str(w, "message").expect(WRITE);
            rmp::encode::write_str(w, &e.to_string()).expect(WRITE);
        }
    }
    out
}

// ---------------------------------------------------------------------------
// depth
// ---------------------------------------------------------------------------

const WRITE: &str = "a Vec never fails to write";
