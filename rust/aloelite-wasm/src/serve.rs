//! The message protocol: `{id, op, args}` in, `{id, ok}` or
//! `{id, error: {code, message}}` out, over anything with `postMessage` and
//! an `onmessage` slot — a Worker's own global scope (`self`), or one end
//! of a `MessageChannel`.
//!
//! `op` and `args` are exactly what [`Fs::call`] takes; `id` is echoed
//! untouched so the page can match replies however it likes. A bytes result
//! is transferred rather than copied. There is no batching, no streaming of
//! partial results and no ordering guarantee beyond the platform's own
//! (which is FIFO per port): the engine is synchronous, so every request is
//! answered before the next is read.

use std::cell::RefCell;
use std::rc::Rc;

use aloelite_core::FsError;
use js_sys::{Array, Function, Object, Reflect, Uint8Array};
use wasm_bindgen::prelude::*;
use web_sys::MessageEvent;

use crate::fs::Fs;
use crate::value;

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// A running server: `stop` to stop answering, `close` to also close the
/// engine handle.
#[wasm_bindgen]
pub struct Server {
    fs: Rc<RefCell<Fs>>,
    endpoint: JsValue,
    handler: Option<Closure<dyn FnMut(MessageEvent)>>,
}

/// Serve `fs` on `endpoint`. Takes the handle: from here on, messages are
/// the only way in, until [`Server::close`].
#[wasm_bindgen]
pub fn serve(fs: Fs, endpoint: JsValue) -> Result<Server, JsValue> {
    let post = Reflect::get(&endpoint, &"postMessage".into())?
        .dyn_into::<Function>()
        .map_err(|_| value::throw_with("usage", "serve: the endpoint has no postMessage"))?;
    let fs = Rc::new(RefCell::new(fs));
    let handler = {
        let fs = fs.clone();
        let endpoint = endpoint.clone();
        Closure::<dyn FnMut(MessageEvent)>::new(move |event: MessageEvent| {
            let reply = handle(&fs, &event.data());
            let _ = post.call2(&endpoint, &reply, &transferables(&reply));
        })
    };
    Reflect::set(
        &endpoint,
        &"onmessage".into(),
        handler.as_ref().unchecked_ref(),
    )?;
    Ok(Server {
        fs,
        endpoint,
        handler: Some(handler),
    })
}

impl std::fmt::Debug for Server {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Server")
            .field("serving", &self.handler.is_some())
            .field("fs", &self.fs.borrow())
            .finish()
    }
}

#[wasm_bindgen]
impl Server {
    /// Stop answering: `onmessage` is cleared. The engine handle stays open.
    pub fn stop(&mut self) {
        if self.handler.take().is_some() {
            let _ = Reflect::set(&self.endpoint, &"onmessage".into(), &JsValue::NULL);
        }
    }

    /// Stop, then close the engine handle (open descriptors aborted, the
    /// admission lock released); resolves once the lock is released.
    pub async fn close(&mut self) -> Result<(), JsValue> {
        self.stop();
        let (result, after) = self.fs.borrow_mut().shut();
        if let Some(after) = after {
            after.await;
        }
        result.map_err(|e| value::throw(&e))
    }
}

// ---------------------------------------------------------------------------
// depth: one request
// ---------------------------------------------------------------------------

fn handle(fs: &Rc<RefCell<Fs>>, request: &JsValue) -> JsValue {
    let id = Reflect::get(request, &"id".into()).unwrap_or(JsValue::UNDEFINED);
    let op = Reflect::get(request, &"op".into())
        .ok()
        .and_then(|v| v.as_string());
    let args = Reflect::get(request, &"args".into()).unwrap_or(JsValue::UNDEFINED);
    let result = match op {
        None => Err(FsError::usage("request has no `op`")),
        Some(op) => match fs.try_borrow_mut() {
            Ok(mut fs) => fs.dispatch(&op, &args),
            Err(_) => Err(FsError::usage("re-entrant request")),
        },
    };
    let reply = Object::new();
    let _ = Reflect::set(&reply, &"id".into(), &id);
    match result {
        Ok(v) => {
            let _ = Reflect::set(&reply, &"ok".into(), &v);
        }
        Err(e) => {
            let _ = Reflect::set(&reply, &"error".into(), &value::error_object(&e));
        }
    }
    reply.into()
}

/// A bytes result moves rather than copies: its buffer is listed as a
/// transferable. It is a fresh buffer the reply alone refers to.
fn transferables(reply: &JsValue) -> Array {
    let list = Array::new();
    if let Ok(ok) = Reflect::get(reply, &"ok".into())
        && let Some(bytes) = ok.dyn_ref::<Uint8Array>()
    {
        list.push(&bytes.buffer());
    }
    list
}
