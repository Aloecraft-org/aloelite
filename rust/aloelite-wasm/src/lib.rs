//! Aloelite in the browser.
//!
//! The deployment shape (doc/DECISIONS.md D-7): the engine runs inside a
//! **Dedicated Worker**, the volume file lives in **OPFS** through
//! `sqlite-wasm-vfs`'s `sahpool` VFS, and this crate is the wasm-bindgen
//! surface the page talks to across `postMessage`. Three consequences that
//! are features, not limitations:
//!
//! - The engine stays synchronous. OPFS sync access handles exist only in
//!   Workers, so putting the engine there is what lets it be the *same*
//!   engine as native — the 91 conformance scenarios run unchanged.
//! - Argon2id (64 MiB / 3 / 4, pinned in format-v1.json) blocks the Worker
//!   for well under a second once per mount, and blocks nothing the user
//!   can see.
//! - One Worker owns one volume. Two tabs are two mounts (ACC-1), and the
//!   browser target is single-writer per volume by a Web Lock — D-4's
//!   admission policy, enforced by the platform.
//!
//! Scaffold: no bindings yet.
