# Conformance suite

Language-agnostic scenarios and format vectors, shared by all four
implementations (Rust, JS/WASM, Python, Kotlin).

The Python suite in `tests/` is the reference *implementation's* test suite: it
is idiomatic pytest, it reaches into schema state, and it is not portable. This
directory is the opposite — data, no code — so a second implementation inherits
the oracle instead of re-deriving it from the Python and hoping the two agree.

```
conformance/
  scenarios/    op sequence -> observable state (this document)
  vectors/      fixed inputs -> exact bytes (see vectors/README.md)
```

Runners live with each implementation. Python's is
`tests/test_conformance_suite.py`.

## What a scenario is

An ordered list of Mount API calls plus assertions about what is observable
*through the API afterwards*. Scenarios never assert on ids, timestamps, or
row layout: those differ between runs and between implementations by design.
They assert on names, types, visibility, sizes, bytes, error codes, and on
identity *relationships* between values captured earlier in the same run.

```yaml
scenario: entry-round-trips-through-a-container
requirements: [NODE-2, IO-1, IO-2]
description: >
  The simplest content path: a container holds an entry, and the bytes written
  are the bytes read back.
steps:
  - op: create_container
    args: {path: /docs}
  - op: create_entry
    args: {path: /docs/a.txt, data: {utf8: "hello"}}
    bind: a
  - op: read_all
    args: {path: /docs/a.txt}
    expect: {utf8: "hello"}
  - op: stat
    args: {path: /docs/a.txt}
    expect: {type: entry, name: a.txt, size: 5, id: {ref: a}}
```

## Scenario fields

| field | meaning |
|---|---|
| `scenario` | unique kebab-case name; the test id |
| `requirements` | requirement ids from `doc/REQUIREMENTS.md` this pins |
| `description` | prose, for humans |
| `setup` | optional volume construction (below) |
| `steps` | ordered list of step objects |

`setup` accepts `chunk_size` (bytes, default 1048576) and `encrypted`
(bool, default false). Small chunk sizes let a scenario cross chunk boundaries
without a megabyte of fixture.

## Step fields

| field | meaning |
|---|---|
| `op` | operation name, exactly as declared in `aloelite/config/mount-api.yaml` |
| `args` | named arguments, using the **spec's** parameter names |
| `bind` | capture this step's return value under a name |
| `expect` | assertion about the return value |
| `raises` | expected error code from the closed set; mutually exclusive with `expect` |

Argument names come from `mount-api.yaml`, not from any implementation. The
spec says `move` takes `from`/`to`; a binding whose function signature says
`src`/`dst` maps them in its runner. Runners are expected to validate scenario
arg names against the spec — Python's does.

## Values

Bytes are always tagged, never bare, so there is no encoding ambiguity across
languages:

| form | meaning |
|---|---|
| `{utf8: "hi"}` | UTF-8 encoded text |
| `{base64: "aGk="}` | arbitrary bytes |
| `{hex: "6869"}` | arbitrary bytes |
| `{repeat: {utf8: "ab"}, count: 4}` | the inner value repeated (`abababab`) |

`{ref: name}` resolves to a value captured earlier by `bind`. It is the only
way to talk about an id: `{id: {ref: a}}` asserts "this is the same node as the
one bound to `a`", which is checkable in any implementation, while a literal
uuid7 is not.

A ref may reach into a field of a bound record with a dot: `{ref: before.created_at}`.
That is how scenarios assert preservation — "copy keeps the source's
`created_at` (OP-4)" is expressible without either side knowing what the
timestamp is.

## Matching

`expect` is a **subset** match. A record assertion names only the fields it
cares about; unnamed fields are unconstrained. This keeps scenarios stable when
a record gains a field.

For `list`, the expectation is a list of entry subsets. Both sides are sorted
by `(name, not visible)` before comparison — visible before hidden within a
name — so a scenario never depends on the storage engine's natural row order.
NODE-5 duplicates are therefore expressed as two entries sharing a name, one
`visible: true` and one `visible: false`.

For bytes, an expectation is a tagged value and must match exactly.

## Adding a scenario

1. Give it a name and the requirement ids it pins.
2. Prefer observable assertions over structural ones. If a scenario can only be
   expressed by reading a table, it belongs in the implementation's own suite,
   not here.
3. Keep it independent: every scenario starts from a fresh volume.
4. Run it. `pytest tests/test_conformance_suite.py -k <scenario-name>`.

A scenario that passes in one implementation and fails in another is either a
bug or an underspecified requirement. Both are worth finding before four
implementations ship.
