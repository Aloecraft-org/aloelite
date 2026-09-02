# Human surfaces

Code in this repo has two readers: the agent maintaining it, and a human
reading a small number of declared surfaces. Write for the maintainer by
default. Write for the human only where this file says a human reads.

## Declared human surfaces

<!-- Per repo. Keep this list short. If a path stays on it that nobody
     reads, remove it. Paths, not descriptions. -->

- `examples/`
- `README.md`

Files not listed here are maintainer-grade: correctness and completeness
win over brevity. Do not apply the surface-only rules below to them.

## Rules for everything (cheap, no correctness cost)

Any file with dense logic opens with a surface block. The surface block
contains, in order, and nothing else:

1. Entry points: public functions, routes, commands, handlers.
2. Configurable values: constants a human might change.
3. Fan-out points: dispatch tables, state transitions, the list of
   implementations behind a polymorphic base.

Nothing that belongs in the surface block may be declared elsewhere in
the file. No constants at first use, no route registered next to its
handler, no dispatch case added inside the dispatching function.

If a file has no natural surface block (logic is multiplexed across
branches, or cases are spread across subclasses), make one: an explicit
dispatch map, a registration block, or a stub listing the implementations
and what distinguishes them. If you cannot write a surface block, say so
rather than skipping it; the file is probably doing too much.

Below the surface block, mark dense sections so a reader knows they may
skip them. A one-line comment such as `# depth: retry and backoff` is
enough. Comments in the surface explain why. Comments in depth may
explain what.

## Rules for declared surfaces only (costly, opt-in)

- Fits on one screen. If it does not, split it or cut it.
- Every line is something the reader would plausibly type themselves.
  Boilerplate they would not type goes in a helper or is elided.
- Error handling, retries, validation, and type annotations may be
  omitted. Each omission is stated in one comment using the marker
  `# example: omits <thing>`, optionally with a pointer to where the
  real version lives.
- The example must still run unchanged against the real API. Do not
  make it readable by making it wrong or by stubbing the interface.
- The reader must be able to retype it from memory after one read.

## Omission marker

`# example: omits ...` (or the language's comment equivalent) is the only
sanctioned way to leave something out. It is only permitted inside
declared surfaces. If it appears anywhere else, that is a bug, not a
style choice: fix the code, do not remove the marker.

## Do not

- Apply surface-only rules to undeclared paths, even if they look like
  they would benefit. Propose adding the path to the list instead.
- Make code "readable" by shortening names, removing branches, or
  dropping cases. Readability is a property of shape, not of size.
- Interleave declarations that belong in the surface block.
- Paper over a missing surface block with a comment that describes one.
