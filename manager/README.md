# manager — the volume manager, in three bounded pieces

The manager grew from "a web page over the engine" into a product with
distinct parts. This layout names them, because the boundaries are the
extraction seams: the manager is headed for its own project, and — more
importantly — the split marks **what a second-language implementation
reimplements versus what it inherits verbatim**.

```
manager/
  api-spec.yaml the HTTP contract, as data — the port's checklist
  api.py        the reference implementation of that contract
  ui/           the HTML manager  — INHERITED verbatim (asset bundle)
  engine/       the Python-engine adapters — REPLACED per language
  errors.py     shared error vocabulary (api <-> engine)
  web.py        entrypoint: dev/standalone server (aloelite-web)
  __main__.py   entrypoint: container mode (preflight + supervisor)
```

## The pieces

**`api-spec.yaml` + `api.py` — the HTTP surface.** The spec is the
cross-language contract for the manager, the way
`aloelite/config/mount-api.yaml` is for the engine: every route with its
params, status codes, and auth gating, plus the credential and CSRF header
names a port must reproduce byte-for-byte. A Rust manager implements that
spec over its own engine, serves the same `ui/` bundle, and the browser
cannot tell the difference.

`api.py` remains the reference implementation, and where the two disagree
api.py is right — `manager/test_api_spec.py` projects the spec onto the
live Flask url_map and fails in BOTH directions (a route added without a
spec entry, or a spec entry for a route that no longer exists), so the
contract cannot quietly rot. `doc/api_testing.md` documents exercising the
surface by hand.

**`ui/` — the HTML manager.** Templates plus vendored static assets
(Bootstrap, Alpine, marked, Prism, signature_pad — see
`ui/static/VENDOR.md` for the no-build-step rationale). Deliberately
language-agnostic: no Python beyond `ui/__init__.py` exposing
`TEMPLATES_DIR`/`STATIC_DIR`. Any server ships this directory verbatim.
The one Jinja construct in use is trivial interpolation; keep it that way
so non-Python servers can render it with any mustache-alike.

**`engine/` — the Python-engine adapter layer.** Everything that binds the
manager to *this* implementation of aloelite: `direct.py` (held in-process
mounts, per-client sessions), `supervisor.py` (FUSE mount child
processes), `preflight.py` (container/deployment checks), `store.py` (the
volume registry). A port replaces this package wholesale; nothing in `ui/`
and nothing in `api.py`'s contract depends on how it is implemented.

## Rules that keep the seams clean

- `ui/` never imports Python; Python addresses it only through
  `manager.ui.TEMPLATES_DIR` / `STATIC_DIR`.
- `api.py` talks to `engine/` through its public classes
  (`DirectSessionRegistry`, `MountSupervisor`, `VolumeStore`) and the
  shared `errors.py` vocabulary — never into engine internals.
- `engine/` never imports `api` or `ui`.
- New UI renderers display untrusted volume content: they go through the
  sandboxed iframe (see `doc/ROADMAP.md`, standing rule).

## Extraction path

When the manager moves to its own repository, the split is mechanical:
this whole directory (with history via git-filter) plus a dependency on
the `aloelite` engine package. The `aloelite` wheel keeps shipping the
manager until then — one distribution through the 0.4 series, two
afterwards. Verified by `manager/test_web_files.py`,
`manager/engine/test_supervisor.py`, `tests/test_direct.py`,
`tests/test_integration.py`, and the real-browser
`script/browser_check.py`.
