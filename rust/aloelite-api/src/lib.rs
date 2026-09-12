//! The Mount API, dispatched by name, over any value type.
//!
//! `aloelite-core` is the engine and `aloelite-store` opens connections to
//! it; this crate is the layer between either of those and a frontend. It
//! answers one question — given an operation's NAME and its arguments by
//! name, run it — and it answers it the same way for every frontend, which
//! is the point. The browser surface and anything that follows it dispatch
//! from one table, through one set of coercions, into one mapping onto
//! `aloelite_core::ops`.
//!
//! | module | what it is |
//! |---|---|
//! | [`table`] | the spec's operations and their parameter names, as data. `tests/projection.rs` holds it against `mount-api.yaml` in both directions |
//! | [`value`] | the two traits a frontend implements: [`Value`] for an argument, [`Surface`] for the pair of value types |
//! | [`args`] | arguments read by name and coerced to the engine's types, written once against [`Value`] |
//! | [`handle`] | [`Handle`], and the dispatch onto `aloelite_core::ops` |
//!
//! A frontend is then small: implement two traits over its own value type,
//! and wrap [`Handle`] in whatever its callers expect.
//!
//! ```ignore
//! let mut h = Handle::new(db);
//! let volumes = h.call::<JsSurface>("list_volumes", &JsValue::UNDEFINED)?;
//! ```
//!
//! Like `aloelite-core`, this crate performs no I/O, never asks which
//! platform it is on, and carries no `cfg`.

pub mod args;
pub mod handle;
pub mod table;
pub mod value;

pub use args::Args;
pub use handle::Handle;
pub use table::{DEFAULT_CHUNK_SIZE, EXTRA_OPS, OPS, Op};
pub use value::{EXTRA_CODES, Surface, Value, code};
