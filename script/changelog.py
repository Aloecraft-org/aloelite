#!/usr/bin/env python3
"""Aloelite changelog tool.

CHANGELOG.yaml is the source of truth for release notes. This renders it
and checks it, so that the release page and the copy of CHANGELOG.md in
the tree are derived from one file rather than maintained in parallel.

Usage:
  script/changelog.py validate              schema and consistency checks
  script/changelog.py render md             whole changelog, as Markdown
  script/changelog.py render md --tag TAG   one release's section only
                                            (what a release body wants)
  script/changelog.py latest                the newest published tag
  script/changelog.py generate              write CHANGELOG.md from the YAML
  script/changelog.py check                 fail unless CHANGELOG.md matches
                                            the YAML; for CI
  script/changelog.py consistency           fail unless the tree agrees with
                                            the newest entry; for CI
  script/changelog.py release-check --tag TAG [--publish]
                                            fail unless TAG is releasable;
                                            prints prerelease= and version=
                                            for GITHUB_OUTPUT

Ported from Diluvium's script/changelog.py, minus the release-mirror half
(aloelite has no mirror, so there is no changelog.json and no mirror flag)
and plus `api_version`, the on-disk schema era, which is aloelite's
break-once field the way LUAC_FORMAT is Diluvium's.

Requires PyYAML, which aloelite already depends on at runtime.
"""

import argparse
import json
import os
import re
import sys

try:
    import yaml
except ImportError:
    sys.exit("changelog.py: PyYAML is required (pip install pyyaml)")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "CHANGELOG.yaml")
MD = os.path.join(ROOT, "CHANGELOG.md")

# keepachangelog's six, in the order it prints them, plus our one.
SECTIONS = [
    ("added", "Added"),
    ("changed", "Changed"),
    ("deprecated", "Deprecated"),
    ("removed", "Removed"),
    ("fixed", "Fixed"),
    ("security", "Security"),
    ("known_issues", "Known issues"),
]
STATUSES = {"released", "unreleased", "tagged"}
SCALARS = {"version", "tag", "date", "status", "stable", "latest",
           "api_version", "summary", "upgrading"}
KNOWN = SCALARS | {k for k, _ in SECTIONS}

# A release's tag is v{version}: unlike Diluvium, this repository carries no
# upstream project's tags, so nothing else can claim the name. An explicit
# `tag:` is still honoured, and checked against the derivation, so a
# one-off can never disagree with itself silently.
def tag_of(r):
    return r.get("tag") or "v%s" % r["version"]


def load():
    with open(SOURCE) as f:
        return yaml.safe_load(f)


def validate(doc):
    """-> list of problems, empty when the file is sound."""
    bad = []
    if doc.get("schema") != 1:
        bad.append("schema must be 1")
    releases = doc.get("releases") or []
    if not releases:
        bad.append("no releases")

    seen_v, seen_t, latest = set(), set(), []
    for r in releases:
        v = r.get("version", "<unnamed>")
        where = "release %s" % v

        for key in r:
            if key not in KNOWN:
                bad.append("%s: unknown key %r" % (where, key))
        for key in ("version", "status", "stable", "summary"):
            if r.get(key) in (None, ""):
                bad.append("%s: missing %s" % (where, key))

        if v in seen_v:
            bad.append("%s: duplicate version" % where)
        seen_v.add(v)

        if r.get("tag") and r["tag"] != "v%s" % v:
            bad.append("%s: explicit tag %r is not v%s -- drop it and let it "
                       "derive, or fix the version" % (where, r["tag"], v))
        tag = tag_of(r) if r.get("version") else None
        if tag:
            if tag in seen_t:
                bad.append("%s: duplicate tag %s" % (where, tag))
            seen_t.add(tag)

        status = r.get("status")
        if status not in STATUSES:
            bad.append("%s: status %r not one of %s"
                       % (where, status, ", ".join(sorted(STATUSES))))

        date = r.get("date")
        if status == "unreleased":
            if date:
                bad.append("%s: unreleased but carries a date" % where)
        elif not date:
            bad.append("%s: %s but has no date" % (where, status))
        elif not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(date)):
            bad.append("%s: date %r is not ISO yyyy-mm-dd" % (where, date))

        if r.get("api_version") is not None:
            if not isinstance(r["api_version"], int):
                bad.append("%s: api_version must be an integer schema era"
                           % where)

        if r.get("latest"):
            latest.append(r)

        # SCALARS enforced by what they are not: YAML resolves a bare
        # `date: 2026-01-01` to a datetime.date, so an allowlist of scalar
        # types would have to name every tag the resolver knows. This is
        # what catches an `upgrading:` written as `- |` instead of `|` --
        # which validates fine as a list and then crashes render.
        for key in sorted(SCALARS):
            val = r.get(key)
            if not isinstance(val, (list, tuple, dict, set)):
                continue
            bad.append("%s: %s must be a single value, not a %s -- a block "
                       "scalar is '%s: |', not '%s:' followed by '- |'"
                       % (where, key, type(val).__name__, key, key))

        for key, _ in SECTIONS:
            items = r.get(key)
            if items is None:
                continue
            if not isinstance(items, list):
                bad.append("%s: %s must be a list" % (where, key))
                continue
            for i, item in enumerate(items):
                if not isinstance(item, str) or not item.strip():
                    bad.append("%s: %s[%d] must be a non-empty string"
                               % (where, key, i))

    if len(latest) != 1:
        bad.append("exactly one release must carry 'latest: true' (found %d)"
                   % len(latest))
    else:
        r = latest[0]
        if not r.get("stable"):
            bad.append("release %s is latest but stable is not true"
                       % r.get("version"))
        if r.get("status") != "released":
            bad.append("release %s is latest but is not released"
                       % r.get("version"))

    for p in doc.get("planned") or []:
        for key in ("title", "summary"):
            if not p.get(key):
                bad.append("planned entry: missing %s" % key)
    return bad


def heading(r):
    date = r.get("date") or "unreleased"
    text = "## [%s] - %s" % (r["version"], date)
    marks = []
    if not r.get("stable"):
        marks.append("prerelease")
    if r.get("status") == "tagged":
        marks.append("tagged, not published")
    if marks:
        text += " (%s)" % ", ".join(marks)
    return text


def bullets(items):
    """A bullet may be multiline; its first line is the headline, and the
    rest is indented under it so Markdown keeps it inside the item."""
    out = []
    for item in items:
        lines = item.rstrip("\n").split("\n")
        out.append("- " + lines[0])
        for line in lines[1:]:
            out.append(("  " + line).rstrip())
    return out


def render_release(r):
    out = [heading(r), ""]
    meta = ["`%s`" % tag_of(r)]
    if r.get("api_version") is not None:
        meta.append("schema era `%d`" % r["api_version"])
    out += [" &middot; ".join(meta), ""]
    if r.get("summary"):
        out += [r["summary"].rstrip("\n"), ""]
    for key, title in SECTIONS:
        if r.get(key):
            out += ["### " + title, ""] + bullets(r[key]) + [""]
    if r.get("upgrading"):
        out += ["### Upgrading", "", r["upgrading"].rstrip("\n"), ""]
    return "\n".join(out).rstrip("\n") + "\n"


def render_planned(doc):
    """Work that is real and scheduled but NOT in this tree. Kept out of
    `releases` on purpose: an entry there claims the code is here, and
    `consistency` checks the newest one against the tree. This section is
    the honest place for a thread that lives on a branch."""
    items = doc.get("planned") or []
    if not items:
        return ""
    out = ["## Planned", ""]
    for p in items:
        out.append("### %s" % p["title"])
        out.append("")
        if p.get("branch"):
            out.append("Not on `main`. Lives on `%s`." % p["branch"])
            out.append("")
        out.append(p["summary"].rstrip("\n"))
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n\n"


def render_md(doc, tag=None):
    if tag:
        for r in doc["releases"]:
            if tag_of(r) == tag:
                return render_release(r)
        sys.exit("changelog.py: no release with tag %r" % tag)
    head = (
        "# Changelog\n\n"
        "All notable changes to Aloelite are recorded here.\n\n"
        "Generated from `CHANGELOG.yaml`, which is the source of truth --\n"
        "edit that file, then run `script/changelog.py generate`.\n\n"
        "The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).\n"
        "`schema era` is the volume's on-disk `api_version`: a file written by\n"
        "one era is readable by a build of that era, and an era bump is a\n"
        "migration rather than a compatible change.\n"
    )
    return head + "\n" + render_planned(doc) + "\n\n".join(
        render_release(r) for r in doc["releases"])


def read(path):
    with open(os.path.join(ROOT, path)) as f:
        return f.read()


def consistency(doc):
    """The newest entry describes the tree as it stands, so the tree has to
    agree with it. The version lives in two files here and drifts quietly --
    `v0.3.6rc1` bumped pyproject.toml and left .technoproj on 0.3.5 -- and
    this is what makes that loud. -> list of problems."""
    bad = []
    r = doc["releases"][0]
    version = r["version"]
    where = "newest entry (%s)" % version

    m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", version)
    if not m:
        bad.append("%s: version is not X.Y.Z" % where)
        return bad
    major, minor, patch = m.groups()

    # pyproject may carry a prerelease suffix while the entry is still
    # unreleased -- 0.3.6rc1 in the tree, 0.3.6 in the changelog, which is
    # the normal state between cutting an rc and publishing. Once the entry
    # is released the two must match exactly.
    got = re.search(r'^version\s*=\s*"([^"]+)"', read("pyproject.toml"), re.M)
    if not got:
        bad.append("pyproject.toml: no version")
    else:
        got = got.group(1)
        if r["status"] == "unreleased":
            if not re.match(r"^%s([abc]|rc|alpha|beta)?\d*$" % re.escape(version),
                            got):
                bad.append("pyproject.toml is %r, which is not %s or a "
                           "prerelease of it (%s)" % (got, version, where))
        elif got != version:
            bad.append("pyproject.toml is %r but %s says %r"
                       % (got, where, version))

    try:
        proj = json.loads(read(".technoproj"))["TECHNO_VERSION"]
    except (OSError, ValueError, KeyError) as e:
        bad.append(".technoproj: cannot read TECHNO_VERSION (%s)" % e)
    else:
        for key, want in (("major", int(major)), ("minor", int(minor)),
                          ("patch", int(patch))):
            if proj.get(key) != want:
                bad.append(".technoproj TECHNO_VERSION.%s is %r but %s implies "
                           "%r" % (key, proj.get(key), where, want))

    # The era the code actually stamps into PRAGMA user_version. An entry
    # claiming an era the build does not write is the mistake worth catching
    # before a migration ships -- it is the one field here that decides
    # whether an existing file still opens.
    if r.get("api_version") is not None:
        era = re.search(r"^SCHEMA_ERA\s*=\s*(\d+)", read("aloelite/db.py"), re.M)
        if not era:
            bad.append("aloelite/db.py: no SCHEMA_ERA")
        elif int(era.group(1)) != r["api_version"]:
            bad.append("db.py SCHEMA_ERA is %s but %s says schema era %s"
                       % (era.group(1), where, r["api_version"]))
    return bad


def release_check(doc, tag, publishing):
    """Gate a release on its changelog entry. -> (problems, outputs)."""
    entry = next((r for r in doc["releases"] if tag_of(r) == tag), None)
    if entry is None:
        return (["no entry in CHANGELOG.yaml for tag %r -- add one before "
                 "releasing it" % tag], {})
    bad = []
    if publishing:
        if entry["status"] != "released":
            bad.append(
                "%s is still status: %s. Before publishing, edit "
                "CHANGELOG.yaml: set status: released and a date, move "
                "latest: true onto it, then re-run "
                "script/changelog.py generate and commit."
                % (tag, entry["status"]))
        if not entry.get("date"):
            bad.append("%s has no date" % tag)
    return bad, {"prerelease": "false" if entry.get("stable") else "true",
                 "version": entry["version"]}


def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("command", choices=["validate", "render", "latest",
                                        "generate", "check", "consistency",
                                        "release-check"])
    ap.add_argument("format", nargs="?", choices=["md"])
    ap.add_argument("--tag")
    ap.add_argument("--publish", action="store_true")
    ap.add_argument("-h", "--help", action="store_true")
    args = ap.parse_args()
    if args.help:
        print(__doc__)
        return 0

    doc = load()
    problems = validate(doc)
    if problems:
        for p in problems:
            print("CHANGELOG.yaml: " + p, file=sys.stderr)
        return 1

    if args.command == "validate":
        print("OK: %d releases, latest=%s"
              % (len(doc["releases"]),
                 next(tag_of(r) for r in doc["releases"] if r.get("latest"))))
    elif args.command == "render":
        sys.stdout.write(render_md(doc, args.tag))
    elif args.command == "latest":
        print(next(tag_of(r) for r in doc["releases"] if r.get("latest")))
    elif args.command == "consistency":
        problems = consistency(doc)
        if problems:
            for p in problems:
                print("inconsistent: " + p, file=sys.stderr)
            return 1
        print("OK: pyproject.toml, .technoproj and db.py agree with %s"
              % doc["releases"][0]["version"])
    elif args.command == "release-check":
        if not args.tag:
            sys.exit("changelog.py: release-check needs --tag")
        problems, out = release_check(doc, args.tag, args.publish)
        if problems:
            for p in problems:
                print("release-check: " + p, file=sys.stderr)
            return 1
        for k, v in out.items():
            print("%s=%s" % (k, v))
    elif args.command in ("generate", "check"):
        text = render_md(doc)
        if args.command == "generate":
            with open(MD, "w") as f:
                f.write(text)
            print("wrote CHANGELOG.md")
        else:
            try:
                with open(MD) as f:
                    current = f.read()
            except OSError:
                current = None
            if current != text:
                print("stale, re-run 'script/changelog.py generate': "
                      "CHANGELOG.md", file=sys.stderr)
                return 1
            print("OK: CHANGELOG.md matches CHANGELOG.yaml")
    return 0


if __name__ == "__main__":
    sys.exit(main())
