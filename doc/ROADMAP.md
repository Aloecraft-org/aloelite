# Roadmap — manager file viewing and editing

<div align="center">

<img src="https://raw.githubusercontent.com/Aloecraft-org/aloelite/refs/heads/main/doc/aloelite.png" style="height:96px; width:96px;"/>

**Aloelite SQLite Filesystem**

[Overview](/README.md) | [Getting Started](/doc/GETTING_STARTED.md) | [Frequently Asked Questions](/doc/FAQ.md)

[Troubleshooting](/doc/TROUBLESHOOTING.md) | [Requirements Spec](/doc/REQUIREMENTS.md) | [Encryption Spec](/doc/ENCRYPTION.md)
</div>

Scope is the web manager's ability to view and edit what is inside a volume.
Everything here was considered alongside the work that shipped the markdown
pane, syntax highlighting, media preview and sketch pad, and deliberately
left out. Each item records *why* it was deferred and what would have to
change to pick it up — the reasons are the useful part.

---

## 1. HTTP Range support on download

**The single highest-value item on this list.**

`_direct_download` (`manager/api.py`) streams a whole file with a fixed
`Content-Length` and no `Accept-Ranges`, so the server cannot answer a `206`.
Browsers need range responses to seek in media, which is why the preview path
currently pulls audio and video wholly into a `blob:` URL before playing: a
blob lives in memory, so the browser serves its own ranges and the scrubber
works. That trick costs RAM proportional to the file and is guarded by a size
confirm at 64 MB.

Range support would replace the trick with the real thing, and pays for
itself elsewhere: resumable downloads, `curl -C -`, and clients that probe
with a ranged request before committing to a transfer.

The engine side already has the necessary shape — `open_read` returns a
descriptor and the streaming loop reads bounded chunks — so this is mostly
request parsing, response headers, and honest handling of multi-range and
unsatisfiable-range requests.

**Blocked on:** nothing. This is ready whenever someone wants it.

## 2. Pan and zoom in the sketch pad

Shipped: pointer/stylus input with pressure, colours, pen width, grid and dot
backgrounds, undo, and SVG output. Not shipped: pinch-zoom and pan.

This is not a small addition. Doing it correctly means a transform matrix
owned by the pad, pointer coordinates inverse-transformed on every sample,
and multi-pointer gesture tracking to separate a two-finger pinch from two
simultaneous strokes. The tempting shortcut — a CSS `transform` on the
`<canvas>` element — is a trap: it scales the rasterised bitmap rather than
the drawing, so strokes blur as soon as the user zooms in, which is precisely
when they wanted detail.

**Blocked on:** deciding whether the pad is a throwaway jotter (current
framing, and pan/zoom is out of character for it) or a real canvas.

## 3. Re-editable sketches

A saved sketch is an SVG: a real, interoperable image that previews through
the existing image path and stays scalable. What it is not is *re-openable* —
reopening gives you shapes, not strokes, so the pad cannot resume editing it.

`signature_pad` already exposes the stroke model via `toData()`/`fromData()`.
The options are a JSON sidecar next to the SVG, the stroke data embedded in
the SVG as a private metadata element, or a sketch-native extension with the
SVG generated on save. The sidecar is simplest; embedding keeps it one file,
which matters more in a filesystem.

**Blocked on:** item 2 — if the pad is not going to grow, re-editing may not
be worth the format complexity.

## 4. CodeMirror 6, and the build-step decision

These are one decision, not two.

CodeMirror 6 is ESM-only: its npm `exports` offers `import` and `require` and
nothing else, and the `codemirror` package is a meta-package over seven ESM
`@codemirror/*` packages. There is no UMD or IIFE build. Adopting it means
adopting a bundler, which the manager has so far avoided on purpose — see
`manager/static/VENDOR.md` for why.

Prism was chosen instead and covers the actual need (read-and-tweak editing
of config and code files) at 64 KB with no build step. What Prism does not
give: bracket matching, folding, multi-cursor, real language services, and an
incremental parse that does not re-highlight the whole buffer per keystroke —
which is why highlighting is capped at 100 KB and degrades to a plain
textarea above it.

**Pick this up when** the manager becomes somewhere people write code rather
than adjust it, or when the 100 KB cap starts being hit in practice. When it
happens, do the bundler properly: `manager/static` is package data shipped in
the wheel, so build output has to be either committed or generated at package
time, and the release path must not require a Node toolchain.

## 5. Splitting `admin.html` further

The Alpine component moved out to `manager/static/admin.js`, which was free —
it contained no Jinja, so it lifted verbatim. The template is now markup plus
a `<style>` block.

If the markup keeps growing, the next step is Jinja `{% include %}` partials
per modal (`templates/partials/*.html`), which stays build-step-free. Beyond
that, splitting `admin.js` itself would need either a shared global namespace
or several `Alpine.data()` registrations.

**Trigger:** the markup passing roughly a thousand lines, or two people
routinely editing different modals at once.

## 6. Office and rich-text formats

- **`.docx`** — `mammoth.js` converts to HTML and would be a genuinely useful
  *read-only* preview through the existing sandboxed iframe.
- **`.xlsx`** — SheetJS, same reasoning, same read-only caveat.
- **`.rtf`** — reading is possible; round-tripping is where it goes wrong.
  Parsing to a rich model, editing, and re-serialising silently drops every
  construct the parser did not understand. A filesystem manager that corrupts
  files on save is worse than one that declines to open them.

**Position:** previews yes, editing no. If the goal is rich text, that is what
the markdown editor is for.

## 7. Image editing

Crop, rotate, resize and annotate need no library — Canvas plus
`toBlob()` through the existing upload is a few hundred lines.

The catch is not the code. Canvas re-encoding is lossy and strips EXIF, so a
"rotate" would silently recompress a photo and discard its capture metadata.
In a *filesystem* tool that is a bad surprise: the user asked to turn a
picture, not to degrade it. Lossless JPEG rotation needs a format-aware
library, and anything with layers and filters (tui.image-editor, filerobot) is
1 MB+ and a different product.

**Blocked on:** deciding whether lossy-with-a-warning is acceptable, or
whether it has to be lossless to ship at all.

## 8. Mermaid diagrams

Wanted, and not close to viable: 83 MB unpacked, ESM-only, with d3, katex,
cytoscape and roughjs underneath. Nothing about that survives contact with
"the manager must be fully self-contained".

**Revisit if** a genuinely small diagram renderer with a plain browser build
appears.

---

## Standing rule for anything on this list

**A new renderer displays untrusted volume content, so it goes through the
sandboxed iframe.** The markdown pane is the reference implementation:
`sandbox=""` blocks script execution, and a `default-src 'none'` CSP inside
the document blocks the off-box subresource loads that sandboxing alone still
permits — a markdown file carrying a tracking pixel would otherwise phone home
the moment someone previewed it. Rendering into the main Alpine DOM instead,
with a sanitiser bolted on, is the weaker option and should not be the way a
future feature arrives.
