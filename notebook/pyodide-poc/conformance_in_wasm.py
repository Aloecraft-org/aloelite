# Run aloelite's own test suite (the portable subset) inside Pyodide.
# The conformance scenarios are the language-agnostic oracle every port must
# pass; running the Python runner on wasm validates Pyodide as a "port"
# without writing one. Excluded: test_fuse_mount (kernel FUSE), test_direct /
# test_store (manager threading/server), which are meaningless in a browser.
import sys

sys.dont_write_bytecode = True  # /repo is a NODEFS view of the real checkout
sys.path.insert(0, "/repo")
sys.path.insert(0, "/poc")

import pyodide_compat

print(pyodide_compat.install())

import os

os.chdir("/repo")

import pytest

SELECT = [
    "tests/test_conformance_suite.py",
    "tests/test_format_vectors.py",
    "tests/test_encryption.py",
    "tests/test_operations.py",
    "tests/test_path.py",
    "tests/test_resolve_containment.py",
    "tests/test_spec_projection.py",
    "tests/test_schema_era.py",
    "tests/test_cli.py",
    "tests/test_admin.py",
]
code = pytest.main(["-q", "-p", "no:cacheprovider", "--tb=short", *SELECT])
print(f"pytest exit code: {code}")
if code != 0:
    raise SystemExit(int(code))
