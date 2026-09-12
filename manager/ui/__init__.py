# ./manager/ui/__init__.py
# License: Apache-2.0 (see manager/engine/__init__.py for the full notice)
"""
manager.ui — the HTML manager, as a self-contained asset bundle.

Templates and vendored static assets only; no Python beyond these two
paths. Any implementation's server ships this directory verbatim and serves
it behind the HTTP contract in manager/api.py — the bundle is deliberately
language-agnostic (see manager/README.md and ui/static/VENDOR.md).
"""

from pathlib import Path as _Path

UI_DIR = _Path(__file__).resolve().parent
TEMPLATES_DIR = UI_DIR / "templates"
STATIC_DIR = UI_DIR / "static"
