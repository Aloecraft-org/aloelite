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
| `harness` | named starting state; default `default` |
| `steps` | ordered list of step objects |

`setup` accepts `chunk_size` (bytes, default 1048576). Small chunk sizes let a
scenario cross chunk boundaries without a megabyte of fixture.

## Harnesses

Some properties need a starting state no operation sequence can build — a
second volume, a second mount, a connection deliberately holding the wrong
cipher. `harness` names one; the runner constructs it and exposes one or more
**named mounts** that steps select with `via`.

| harness | provides |
|---|---|
| `default` | one plain volume, one mount named `default` |
| `two_mounts_one_volume` | one plain volume, two connections, mounts `first` and `second` (both rw; the second passes the D-4 overlap opt-in) |
| `ro_and_rw_mounts` | one plain volume, mounts `rw` and `ro` (ACC-1b access modes) |
| `attach_without_key` | an encrypted volume with content, then a connection bound to its mount with no PIN |
| `keyed_cipher_plain_volume` | a connection holding a volume key, pointed at a mount on a plain volume |
| `two_entries_same_bytes_convergent` | encrypted volume, two entries with identical bytes |
| `two_entries_same_bytes_random` | the same under `enc_mode: random` |

A runner that has not implemented a harness must **skip** the scenario. Silently
passing it reports conformance nobody checked.

## Step fields

| field | meaning |
|---|---|
| `op` | operation name, exactly as declared in `aloelite/config/mount-api.yaml` |
| `args` | named arguments, using the **spec's** parameter names |
| `via` | which named mount to act through; default `default`, else `first` |
| `bind` | capture this step's return value under a name |
| `expect` | assertion about the return value |
| `raises` | expected error code from the closed set; mutually exclusive with `expect` |

## Inspections

A few properties are deliberately invisible through the Mount API and can only
be asserted by looking at storage. Dedup is the case that matters: `content.yaml`
requires that sharing be *unobservable*, which is exactly why convergent and
random `enc_mode` cannot be told apart through any operation.

Those steps use an **inspection** in place of an `op`. Inspections are not Mount
API operations and every runner must implement them by reaching past the API:

| inspection | asserts |
|---|---|
| `assert_pool_rows` | `count` rows in the chunk pool |

## A YAML trap worth knowing

Never use a bare `on`, `off`, `yes`, `no`, `true`, or `false` as a key. YAML 1.1
(PyYAML) reads them as booleans; YAML 1.2 (Rust `serde_yaml`, Go `yaml.v3`)
reads them as strings. The same fixture would then mean two different things in
two implementations — the exact failure this directory exists to prevent. This
bit us during authoring: a step keyed `on: second` parsed as the key `True`,
both steps ran on the same mount, and a lock-contention scenario passed by
never contending. The runner's `test_no_scenario_key_is_a_yaml_boolean` guards
the whole class; keep the equivalent in every runner.

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
