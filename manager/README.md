# manager — the volume manager, in three bounded pieces

The manager grew from "a web page over the engine" into a product with
distinct parts. This layout names them, because the boundaries are the
extraction seams: the manager is headed for its own project, and — more
importantly — the split marks **what a second-language implementation
reimplements versus what it inherits verbatim**.

```
manager/
  api.py        the HTTP contract — REIMPLEMENTED per language
  ui/           the HTML manager  — INHERITED verbatim (asset bundle)
  engine/       the Python-engine adapters — REPLACED per language
  errors.py     shared error vocabulary (api <-> engine)
  web.py        entrypoint: dev/standalone server (aloelite-web)
  __main__.py   entrypoint: container mode (preflight + supervisor)
```

## The pieces

**`api.py` — the HTTP surface.** Routes, request/response shapes, auth
(bearer token + cookie compat), and the streaming download path. This file
*is* the cross-language contract for the manager, the way
`aloelite/config/mount-api.yaml` is for the engine: a Rust manager
re-exposes these routes over its own engine and serves the same `ui/`
bundle, and the browser cannot tell the difference. (`doc/api_testing.md`
documents the surface; a future step is extracting the route inventory
into a spec file the way mount-api.yaml did it.)

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
