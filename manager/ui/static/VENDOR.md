# Vendored front-end assets

The manager must be **fully self-contained**. A CDN reference would leak page
loads off a private deployment and break the UI entirely where the VPN'd
client has no other egress, so every asset the admin page needs is committed
here and served from `/static/`.

There is no bundler. That is a constraint, not an oversight: `aloelite-web`
ships as part of a pip package, and a build step would mean either committing
build output anyway or growing a Node toolchain in the release path. So an
asset qualifies for this directory only if it has a **plain browser build** —
something a `<script src>` can load with no import map, no module resolution,
and no network at runtime.

That rule is what picked most of the libraries below, and it is the first
thing to check before adding another.

| File | Upstream | Version | License | Bytes |
|---|---|---|---|---|
| `alpine.min.js` | [Alpine.js](https://alpinejs.dev) | 3.x | MIT | 44,799 |
| `bootstrap.bundle.min.js` | [Bootstrap](https://getbootstrap.com) | 5.x | MIT | 80,496 |
| `bootstrap.min.css` | [Bootstrap](https://getbootstrap.com) | 5.x | MIT | 232,111 |
| `marked.js` | [marked](https://github.com/markedjs/marked) | 18.0.9 | MIT | 43,821 |
| `prism.js` | [Prism](https://prismjs.com) | 1.30.0 | MIT | 64,008 |
| `prism.css` | Prism default theme | 1.30.0 | MIT | 1,789 |
| `signature_pad.js` | [signature_pad](https://github.com/szimek/signature_pad) | 5.1.4 | MIT | 16,703 |

`admin.js` is ours, not vendored — it is the Alpine component behind
`templates/admin.html`.

## Provenance

Each file is copied unmodified from the published npm tarball, except
`prism.js` (see below):

    npm pack marked@18.0.9
    tar xzf marked-18.0.9.tgz
    cp package/lib/marked.umd.js manager/static/marked.js

    npm pack signature_pad@5.1.4
    cp package/dist/signature_pad.umd.min.js manager/static/signature_pad.js

## Rebuilding `prism.js`

Prism has no single prebuilt file covering an arbitrary language set, so this
one is concatenated from `prismjs/components/*.min.js`:

    prism-core.min.js
      + markup, css, clike, javascript, typescript, python, yaml, json, bash,
        rust, go, c, sql, toml, ini, markdown, docker, lua, diff, properties,
        ruby, java, markup-templating, php, perl, git

**Order matters, and a missing dependency is not a quiet degradation.**
Resolve the `require` graph from `prismjs/components.json` before
concatenating. `markup-templating` is in that list only because `php` requires
it — and `prism-php` registers a `before-tokenize` hook that runs on *every*
`Prism.highlight()` call, so omitting it threw on all languages, not just PHP.
That failure mode is why the bundle is built from the dependency graph rather
than a hand-written list.

The bundle sets `Prism.manual = true` before core loads: the editor overlay
calls `Prism.highlight()` itself and must not have Prism auto-scanning the
page on `DOMContentLoaded`.

## Why not the obvious alternatives

- **CodeMirror 6** — ESM-only. Its npm `exports` field offers `import` and
  `require` and nothing else, and it is a meta-package over seven ESM
  `@codemirror/*` packages. There is no UMD or IIFE build, so adopting it
  means adopting a bundler first. Deferred; see `doc/ROADMAP.md`.
- **CodeMirror 5** — has a plain global build, but npm ships it unminified:
  402 KB of core plus ~280 KB of modes for the languages the editor accepts.
  Prism does the same job here for a tenth of that.
- **markdown-it** — does ship `dist/browser/markdown-it.umd.min.js`, so it
  would work; marked won on size (43 KB against 114 KB) and on having zero
  runtime dependencies.
- **DOMPurify** — not needed. Rendered markdown goes into an
  `<iframe sandbox="">` with a `default-src 'none'` CSP, which blocks script
  execution *and* the off-box subresource loads that sandboxing alone would
  still permit. See `_mdDoc` in `admin.js`.
- **Mermaid** — 83 MB unpacked, ESM-only, and pulls in d3, katex, cytoscape
  and roughjs. Not a candidate under any reading of "self-contained".
