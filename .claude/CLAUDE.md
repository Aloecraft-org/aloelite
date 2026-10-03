# Operator

Aloelite is a filesystem stored in a single SQLite file: a Python
reference engine (`aloelite/`), a web volume manager (`manager/`), and a
Rust port of the same engine (`rust/`, eight crates) that runs natively,
in the browser, under WASI and as an Extism plug-in. Both engines are held
to one spec by the conformance suite in `conformance/`. It ships to PyPI,
GitHub releases and Docker Hub. The owner is @aloecraft.

Every run needs:

- Python: `pip install -e .[test]`, then `pytest` and
  `ruff check . && ruff format --check .`.
- Rust, from `rust/`: `cargo fmt --all -- --check`,
  `cargo clippy --workspace --all-targets -- -D warnings`,
  `cargo test --workspace`. The WASI targets need wasi-sdk; see the
  `rust` job in `.github/workflows/main.yml`.
- Release metadata: `technoproj check`, `technoproj-changelog check` and
  `technoproj-changelog consistency`. `CHANGELOG.yaml` is the facts
  source; `CHANGELOG.md` and `changelog.json` are generated from it.
- The release process is `doc/RELEASING.md`, held to `doc/ALIGNMENT.md`,
  which is a byte-identical copy of technoproj's.

The operating protocol is .claude/rules/operating.md. It and every
other file in .claude/rules/ apply to every run.
