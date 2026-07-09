#!/usr/bin/env python3
"""
pydocgen.py - minimal static HTML documentation generator for Python source.

Walks a source tree, parses each .py file with the `ast` module (no imports
of your code required, so it works even on packages with heavy/missing
dependencies), and emits one HTML page per module plus an index.html linking
them all. Output is plain semantic HTML + one shared CSS file, ready to
rsync/copy into an Apache docroot.

Usage:
    python3 pydocgen.py SRC_DIR OUT_DIR [--title "My Project Docs"]

Example:
    python3 pydocgen.py ./mypackage ./docs_out --title "aloelite-py"
    # then: cp -r docs_out/* /var/www/html/docs/
"""

import argparse
import ast
import html
import os
import sys
from pathlib import Path

CSS = """
:root { --fg:#1d1f21; --muted:#6b7280; --bg:#fff; --accent:#2563eb; --code-bg:#f3f4f6; --border:#e5e7eb; }
* { box-sizing: border-box; }
body { margin:0; font-family: -apple-system, Segoe UI, Helvetica, Arial, sans-serif; color:var(--fg); background:var(--bg); }
.layout { display:flex; min-height:100vh; }
nav.sidebar { width:260px; flex:0 0 260px; border-right:1px solid var(--border); padding:1.5rem 1rem; overflow-y:auto; }
nav.sidebar h2 { font-size:0.85rem; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); margin:1.25rem 0 .5rem; }
nav.sidebar a { display:block; padding:.25rem 0; color:var(--fg); text-decoration:none; font-size:0.9rem; }
nav.sidebar a:hover { color:var(--accent); text-decoration:underline; }
main { flex:1; padding:2rem 3rem; max-width:900px; }
h1 { font-size:1.6rem; margin-bottom:.25rem; }
h1 .kind { color:var(--muted); font-weight:400; font-size:1rem; }
.module-doc { color:#374151; margin:0.5rem 0 2rem; white-space:pre-wrap; }
section.item { border-top:1px solid var(--border); padding:1.25rem 0; }
section.item h3 { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size:1rem; margin:0 0 .4rem; }
section.item .sig { color:var(--accent); }
section.item .doc { white-space:pre-wrap; color:#374151; margin:.5rem 0 0; font-size:0.92rem; }
.badge { display:inline-block; font-size:0.7rem; padding:0 .4rem; border-radius:3px; background:var(--code-bg); color:var(--muted); margin-left:.4rem; }
code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.empty { color:var(--muted); font-style:italic; }
a.back { font-size:0.85rem; color:var(--muted); text-decoration:none; }
a.back:hover { text-decoration:underline; }
.readme { padding-bottom:1.5rem; border-bottom:1px solid var(--border); margin-bottom:1.5rem; }
.readme h1 { font-size:1.8rem; }
.readme h2 { font-size:1.3rem; margin-top:1.5rem; }
.readme h3 { font-size:1.05rem; }
.readme p { line-height:1.55; color:#374151; }
.readme code { background:var(--code-bg); padding:.1rem .3rem; border-radius:3px; font-size:0.9em; }
.readme pre { background:var(--code-bg); padding:.75rem 1rem; border-radius:6px; overflow-x:auto; }
.readme pre code { background:none; padding:0; }
.readme table { border-collapse:collapse; margin:1rem 0; }
.readme th, .readme td { border:1px solid var(--border); padding:.4rem .7rem; }
.readme img { max-width:100%; }
.readme blockquote { border-left:3px solid var(--border); margin:0; padding:.2rem 1rem; color:var(--muted); }
"""


def get_signature(node):
    args = []
    a = node.args
    defaults = [None] * (len(a.args) - len(a.defaults)) + a.defaults
    for arg, default in zip(a.args, defaults):
        s = arg.arg
        if default is not None:
            try:
                s += f"={ast.unparse(default)}"
            except Exception:
                s += "=..."
        args.append(s)
    if a.vararg:
        args.append(f"*{a.vararg.arg}")
    elif a.kwonlyargs:
        args.append("*")
    for arg, default in zip(a.kwonlyargs, a.kw_defaults):
        s = arg.arg
        if default is not None:
            try:
                s += f"={ast.unparse(default)}"
            except Exception:
                s += "=..."
        args.append(s)
    if a.kwarg:
        args.append(f"**{a.kwarg.arg}")
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {node.name}({', '.join(args)})"


def collect_module(path: Path):
    """Parse one .py file and return a dict describing its public API."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as e:
        return {"error": str(e)}

    mod_doc = ast.get_docstring(tree)
    classes, functions = [], []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue
            functions.append(
                {
                    "name": node.name,
                    "sig": get_signature(node),
                    "doc": ast.get_docstring(node) or "",
                }
            )
        elif isinstance(node, ast.ClassDef):
            if node.name.startswith("_"):
                continue
            methods = []
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if sub.name.startswith("_") and sub.name != "__init__":
                        continue
                    methods.append(
                        {
                            "name": sub.name,
                            "sig": get_signature(sub),
                            "doc": ast.get_docstring(sub) or "",
                        }
                    )
            bases = []
            for b in node.bases:
                try:
                    bases.append(ast.unparse(b))
                except Exception:
                    bases.append("?")
            classes.append(
                {
                    "name": node.name,
                    "bases": bases,
                    "doc": ast.get_docstring(node) or "",
                    "methods": methods,
                }
            )

    return {"doc": mod_doc or "", "classes": classes, "functions": functions}


def render_item(kind, name, sig, doc, badge=None):
    badge_html = f'<span class="badge">{html.escape(badge)}</span>' if badge else ""
    sig_html = f'<span class="sig">{html.escape(sig)}</span>' if sig else ""
    doc_html = html.escape(doc) if doc else '<span class="empty">No docstring.</span>'
    return f"""<section class="item">
  <h3>{kind} <code class="mono">{html.escape(name)}</code>{badge_html}</h3>
  {f'<div class="mono">{sig_html}</div>' if sig else ""}
  <div class="doc">{doc_html}</div>
</section>"""


def render_module_page(project_title, mod_name, info, all_modules):
    sidebar = "\n".join(
        f'<a href="{m}.html"{' style="font-weight:600"' if m == mod_name else ""}>{html.escape(m)}</a>'
        for m in all_modules
    )
    # module pages live in out/modules/, so css and the home page are one level up

    if "error" in info:
        body = f'<p class="empty">Could not parse this file: {html.escape(info["error"])}</p>'
    else:
        parts = []
        if info["doc"]:
            parts.append(f'<p class="module-doc">{html.escape(info["doc"])}</p>')
        if info["classes"]:
            parts.append("<h2>Classes</h2>")
            for c in info["classes"]:
                bases = f"({', '.join(c['bases'])})" if c["bases"] else ""
                parts.append(render_item("class", c["name"] + bases, "", c["doc"]))
                for m in c["methods"]:
                    parts.append(render_item("method", m["name"], m["sig"], m["doc"]))
        if info["functions"]:
            parts.append("<h2>Functions</h2>")
            for f in info["functions"]:
                parts.append(render_item("function", f["name"], f["sig"], f["doc"]))
        if not info["classes"] and not info["functions"]:
            parts.append('<p class="empty">No public classes or functions found.</p>')
        body = "\n".join(parts)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<link rel="icon" type="image/svg+xml" href="/favicon.svg" />
<title>{html.escape(mod_name)}</title>
<link rel="stylesheet" href="../style.css">
</head>
<body>
<div class="layout">
<nav class="sidebar">
<h1>{html.escape(project_title)}</h1>
<a class="back" href="/"><strong>&larr; Documentation Home</strong></a>
<h2>Modules</h2>
{sidebar}
</nav>
<main>
<h1>{html.escape(mod_name)} <span class="kind">module</span></h1>
{body}
</main>
</div>
</body>
</html>"""


def render_index(project_title, all_modules, readme_html=None):
    sidebar = "\n".join(
        f'<a href="modules/{m}.html">{html.escape(m)}</a>' for m in all_modules
    )
    links = "\n".join(
        f'<li><a href="modules/{m}.html"><code class="mono">{html.escape(m)}</code></a></li>'
        for m in all_modules
    )
    readme_block = f'<div class="readme">{readme_html}</div>' if readme_html else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<link rel="icon" type="image/svg+xml" href="/favicon.svg" />
<title>{html.escape(project_title)}</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
<div class="layout">
<nav class="sidebar">
<h1>{html.escape(project_title)}</h1>
<a class="back" href="/"><strong>Documentation Home</strong></a>
<h2>Modules</h2>
{sidebar}
</nav>
<main>
{readme_block}
<h2>Modules</h2>
<p class="module-doc">Generated documentation for {len(all_modules)} module(s).</p>
<ul>
{links}
</ul>
</main>
</div>
</body>
</html>"""


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("src", help="Source directory to scan for .py files")
    ap.add_argument("out", help="Output directory for generated HTML")
    ap.add_argument(
        "--title", default="Project Documentation", help="Title for the index page"
    )
    ap.add_argument(
        "--readme",
        help="Path to a raw HTML fragment (e.g. from "
        "'pandoc README.md -o readme.html -f markdown+emoji') "
        "to embed on the home page",
    )
    ap.add_argument(
        "--exclude",
        nargs="*",
        default=["test", "tests", "__pycache__", "venv", ".venv"],
        help="Directory names to skip",
    )
    args = ap.parse_args()

    src = Path(args.src).resolve()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    py_files = []
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in args.exclude and not d.startswith(".")]
        for f in files:
            if f.endswith(".py"):
                py_files.append(Path(root) / f)

    if not py_files:
        print(f"No .py files found under {src}", file=sys.stderr)
        sys.exit(1)

    modules = {}
    for path in sorted(py_files):
        rel = path.relative_to(src).with_suffix("")
        mod_name = ".".join(rel.parts)
        if mod_name.endswith("__init__"):
            mod_name = mod_name.rsplit(".", 1)[0] or path.parent.name
        modules[mod_name] = collect_module(path)

    mod_names = sorted(modules.keys())

    modules_dir = out / "modules"
    modules_dir.mkdir(parents=True, exist_ok=True)

    readme_html = None
    if args.readme:
        readme_path = Path(args.readme)
        if not readme_path.exists():
            print(f"Warning: --readme path not found: {readme_path}", file=sys.stderr)
        else:
            readme_html = readme_path.read_text(encoding="utf-8")

    (out / "style.css").write_text(CSS, encoding="utf-8")
    (out / "index.html").write_text(
        render_index(args.title, mod_names, readme_html), encoding="utf-8"
    )
    for mod_name, info in modules.items():
        page = render_module_page(args.title, mod_name, info, mod_names)
        (modules_dir / f"{mod_name}.html").write_text(page, encoding="utf-8")

    print(
        f"Wrote {len(mod_names)} module page(s) to {modules_dir} and index.html to {out}"
    )


if __name__ == "__main__":
    main()
