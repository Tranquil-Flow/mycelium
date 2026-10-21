#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Rebuild the public Mycelium history: origin/main plus one commit per feature.

Given the published ``origin/main`` commit and a *source tree* commit ``T`` (the
release candidate, already licensed, scrubbed and carrying SPDX headers), this
script creates a NEW local branch that starts at ``origin/main`` and adds one
commit per feature from ``public-features.json``.

* The set of files to publish is ``T`` minus the excludes (``--excludes`` file
  plus ``always_exclude`` in the feature file).
* Every path that differs between ``origin/main`` and that set must be matched by
  exactly one feature (``paths`` globs minus ``except`` globs). Unassigned or
  doubly-assigned paths abort the run and are printed.
* Commit N's tree is commit N-1's tree plus that feature's final files from ``T``.
  Every blob therefore comes from ``T``; nothing is re-written.
* Commits are written with git plumbing and a private index: no working tree,
  no checkout, no fetch, no push. Author and committer are fixed, there are no
  trailers except the one given with ``--co-author``, and every commit carries the
  same timestamp (the time of the run unless ``--date`` is given).
* ``--co-author 'Name <email>'`` appends exactly one ``Co-authored-by: Name <email>``
  trailer, after a blank line, to every generated commit message. Feature titles and
  bodies still may not contain a trailer themselves.
* After the last commit the tree must equal ``T`` minus the excludes.

Glob syntax: ``*`` and ``?`` do not cross ``/``; ``[0-9]`` style classes work; ``**`` crosses it; a pattern
without a wildcard matches that exact path.

Usage::

    python3 scripts/release/build_public_history.py \\
        --origin-main <sha> --source <commit-or-ref> --branch publish/v0.1.0 \\
        --excludes <path-to-excludes.txt> \\
        --co-author 'Name <email>'

    ... --check          only classify and report, create nothing
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import OrderedDict
from datetime import datetime, timezone

AUTHOR_NAME = "Tranquil-Flow"
AUTHOR_EMAIL = "tranquil_flow@protonmail.com"
TITLE_RE = re.compile(r"^(feat|fix|docs|chore|test|build|ci|refactor|perf)(\([a-z0-9._-]+\))?: \S.{3,90}$")
TRAILER_RE = re.compile(r"^[A-Za-z][A-Za-z-]+:\s", re.MULTILINE)
FORBIDDEN_MESSAGE_WORDS = ("co-authored-by", "signed-off-by", "claude", "anthropic", "generated with")
CO_AUTHOR_RE = re.compile(r"^[^<>\r\n:]+ <[^<>\s@]+@[^<>\s@]+>$")
PLACEHOLDER_RE = re.compile(rb"@@[A-Z0-9_]+@@")
ZERO_OID = "0" * 40
DEFAULT_FEATURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public-features.json")


class BuildError(Exception):
    pass


# --------------------------------------------------------------------------- git


def git(repo, *args, env=None, data=None, check=True):
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    proc = subprocess.run(
        ["git", "-C", repo, *args],
        input=data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=full_env,
    )
    if check and proc.returncode != 0:
        raise BuildError("git %s failed: %s" % (" ".join(args[:3]), proc.stderr.decode(errors="replace").strip()))
    return proc.stdout


def git_text(repo, *args, **kw):
    return git(repo, *args, **kw).decode("utf-8", "replace").strip()


def resolve_commit(repo, rev, label):
    out = git(repo, "rev-parse", "--verify", "--quiet", rev + "^{commit}", check=False).decode().strip()
    if not out:
        raise BuildError("%s %r does not resolve to a commit in %s" % (label, rev, repo))
    return out


def ls_tree(repo, rev):
    """path -> (mode, oid) for every blob in ``rev``."""
    raw = git(repo, "ls-tree", "-r", "-z", "--full-tree", rev)
    out = {}
    for rec in raw.split(b"\0"):
        if not rec:
            continue
        meta, path = rec.split(b"\t", 1)
        mode, kind, oid = meta.decode().split()
        if kind != "blob":
            raise BuildError("unsupported tree entry %s %s" % (kind, path.decode(errors="replace")))
        out[path.decode("utf-8")] = (mode, oid)
    return out


# -------------------------------------------------------------------------- globs


def glob_to_regex(pattern):
    i, n, out = 0, len(pattern), []
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern[i : i + 2] == "**":
                i += 2
                if pattern[i : i + 1] == "/":
                    i += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[" and "]" in pattern[i + 1 :]:
            j = pattern.index("]", i + 1)
            out.append("[" + pattern[i + 1 : j].replace("\\", "\\\\") + "]")
            i = j
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


class Matcher:
    def __init__(self, patterns):
        self.patterns = list(patterns)
        self.regexes = [glob_to_regex(p) for p in self.patterns]

    def __call__(self, path):
        return any(r.match(path) for r in self.regexes)


# ------------------------------------------------------------------- feature file


def load_features(path):
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if doc.get("schema") != "mycelium.public-features.v1":
        raise BuildError("feature file schema must be mycelium.public-features.v1")
    features = doc.get("features")
    if not isinstance(features, list) or not features:
        raise BuildError("feature file has no features")
    seen = set()
    for feat in features:
        for key in ("id", "title", "body", "paths"):
            if key not in feat:
                raise BuildError("feature %r lacks %r" % (feat.get("id"), key))
        if feat["id"] in seen:
            raise BuildError("duplicate feature id %s" % feat["id"])
        seen.add(feat["id"])
        if not TITLE_RE.match(feat["title"]):
            raise BuildError("feature %s: title is not conventional-commit style: %r" % (feat["id"], feat["title"]))
        message = feat["title"] + "\n\n" + feat["body"]
        low = message.lower()
        for word in FORBIDDEN_MESSAGE_WORDS:
            if word in low:
                raise BuildError("feature %s: message contains forbidden text %r" % (feat["id"], word))
        body_lines = feat["body"].strip().splitlines()
        if any(TRAILER_RE.match(ln) for ln in body_lines[-1:]) and ":" in body_lines[-1][:40]:
            raise BuildError("feature %s: last body line looks like a trailer" % feat["id"])
        if not feat["paths"]:
            raise BuildError("feature %s has no paths" % feat["id"])
    return doc, features


def co_author_trailer(value):
    """``Name <email>`` -> the single trailer line; refuses anything else."""
    if value is None:
        return None
    if not CO_AUTHOR_RE.match(value) or value != value.strip():
        raise BuildError("--co-author must be exactly 'Name <email>', got %r" % value)
    low = value.lower()
    for word in FORBIDDEN_MESSAGE_WORDS:
        if word in low:
            raise BuildError("--co-author contains forbidden text %r" % word)
    return "Co-authored-by: " + value


def read_excludes(path):
    out = set()
    if not path:
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                out.add(line)
    return out


# --------------------------------------------------------------------- classify


def classify(changed, features):
    """Return (assignment, unassigned, ambiguous). assignment: id -> [paths]."""
    matchers = [(f["id"], Matcher(f["paths"]), Matcher(f.get("except", []))) for f in features]
    assignment = OrderedDict((f["id"], []) for f in features)
    unassigned, ambiguous = [], []
    for path in sorted(changed):
        hits = [fid for fid, inc, exc in matchers if inc(path) and not exc(path)]
        if not hits:
            unassigned.append(path)
        elif len(hits) > 1:
            ambiguous.append((path, hits))
        else:
            assignment[hits[0]].append(path)
    return assignment, unassigned, ambiguous


def dead_patterns(features, universe):
    """Patterns that match no path at all (stale entries in the feature file)."""
    dead = []
    for feat in features:
        for pat in feat["paths"]:
            rx = glob_to_regex(pat)
            if not any(rx.match(p) for p in universe):
                dead.append((feat["id"], pat))
    return dead


# ------------------------------------------------------------------------- build


def index_info(entries):
    lines = []
    for path, val in entries:
        mode, oid = val if val else ("0", ZERO_OID)
        lines.append("%s %s\t%s\0" % (mode, oid, path))
    return "".join(lines).encode("utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--repo", default=".", help="repository holding origin/main and the source commit")
    ap.add_argument("--origin-main", required=True, help="published origin/main commit (sha or ref)")
    ap.add_argument("--source", required=True, help="source tree commit T (sha or ref)")
    ap.add_argument("--branch", default="publish/v0.1.0", help="new local branch to create")
    ap.add_argument("--features", default=DEFAULT_FEATURES)
    ap.add_argument("--excludes", help="file of repo-relative paths to keep out of the public tree")
    ap.add_argument("--date", help="commit date, ISO 8601 (default: now, one value for the whole run)")
    ap.add_argument("--check", action="store_true", help="classify and report only; create nothing")
    ap.add_argument("--replace-branch", action="store_true", help="overwrite the target branch if it exists")
    ap.add_argument("--allow-placeholder", action="store_true", help="allow @@TOKEN@@ placeholders in the tree")
    ap.add_argument("--co-author", metavar="'NAME <EMAIL>'", help="append one Co-authored-by trailer to every commit message")
    ap.add_argument("--report", help="write the JSON report here")
    args = ap.parse_args(argv)

    repo = os.path.abspath(args.repo)
    origin = resolve_commit(repo, args.origin_main, "--origin-main")
    source = resolve_commit(repo, args.source, "--source")
    doc, features = load_features(args.features)
    trailer = co_author_trailer(args.co_author)

    excludes = read_excludes(args.excludes) | set(doc.get("always_exclude_paths", []))
    excl_globs = Matcher(doc.get("always_exclude_globs", []))

    src_tree = ls_tree(repo, source)
    base_tree = ls_tree(repo, origin)
    stale_excludes = sorted(p for p in excludes if p not in src_tree)
    public = OrderedDict((p, v) for p, v in sorted(src_tree.items()) if p not in excludes and not excl_globs(p))
    leaked_base = sorted(p for p in base_tree if p in excludes or excl_globs(p))
    if leaked_base:
        raise BuildError("excluded paths already exist in origin/main: %s" % ", ".join(leaked_base[:10]))

    changed = [p for p in public if base_tree.get(p) != public[p]]
    deleted = [p for p in base_tree if p not in public]
    universe = set(changed) | set(deleted)
    assignment, unassigned, ambiguous = classify(universe, features)

    report = OrderedDict()
    report["origin_main"] = origin
    report["source"] = source
    report["source_files"] = len(src_tree)
    report["excluded_present_in_source"] = len(src_tree) - len(public)
    report["excluded_not_in_source"] = stale_excludes
    report["public_files"] = len(public)
    report["unchanged_from_origin_main"] = len(public) - len(changed)
    report["changed_vs_origin_main"] = len(changed)
    report["deleted_vs_origin_main"] = len(deleted)
    report["unassigned"] = unassigned
    report["ambiguous"] = [{"path": p, "features": h} for p, h in ambiguous]
    report["files_per_feature"] = OrderedDict((fid, len(paths)) for fid, paths in assignment.items())
    report["dead_patterns"] = [{"feature": f, "pattern": p} for f, p in dead_patterns(features, universe)]

    ok = not unassigned and not ambiguous
    if not ok:
        print("REFUSING: every changed path must belong to exactly one feature.", file=sys.stderr)
        for p in unassigned[:200]:
            print("  unassigned: " + p, file=sys.stderr)
        for p, h in ambiguous[:200]:
            print("  ambiguous:  %s -> %s" % (p, ", ".join(h)), file=sys.stderr)
        if len(unassigned) > 200:
            print("  ... %d more unassigned" % (len(unassigned) - 200), file=sys.stderr)
        _emit(report, args.report)
        return 2

    if not args.allow_placeholder:
        marked = []
        for path, (mode, oid) in public.items():
            if path.endswith((".md", ".txt", ".json")) and path.startswith(("docs/RELEASE", "README")):
                if PLACEHOLDER_RE.search(git(repo, "cat-file", "blob", oid)):
                    marked.append(path)
        if marked:
            print("REFUSING: unresolved @@PLACEHOLDER@@ in: " + ", ".join(marked), file=sys.stderr)
            print("Fill them in (for example the release asset SHA-256) or pass --allow-placeholder.", file=sys.stderr)
            return 3

    if args.check:
        _emit(report, args.report)
        return 0

    ref = "refs/heads/" + args.branch
    exists = git(repo, "rev-parse", "--verify", "--quiet", ref, check=False).decode().strip()
    if exists and not args.replace_branch:
        raise BuildError("branch %s already exists (%s); use --replace-branch" % (args.branch, exists[:12]))

    when = args.date or datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    env = {
        "GIT_AUTHOR_NAME": AUTHOR_NAME,
        "GIT_AUTHOR_EMAIL": AUTHOR_EMAIL,
        "GIT_COMMITTER_NAME": AUTHOR_NAME,
        "GIT_COMMITTER_EMAIL": AUTHOR_EMAIL,
        "GIT_AUTHOR_DATE": when,
        "GIT_COMMITTER_DATE": when,
    }

    commits = []
    with tempfile.TemporaryDirectory(prefix="mycelium-public-history-") as tmp:
        idx = os.path.join(tmp, "index")
        ienv = dict(env, GIT_INDEX_FILE=idx)
        git(repo, "read-tree", origin, env=ienv)
        parent = origin
        parent_tree = git_text(repo, "rev-parse", origin + "^{tree}")
        for feat in features:
            paths = assignment[feat["id"]]
            if not paths:
                print("note: feature %s has no changes; no commit" % feat["id"], file=sys.stderr)
                continue
            entries = [(p, public.get(p)) for p in paths]
            git(repo, "update-index", "-z", "--index-info", env=ienv, data=index_info(entries))
            tree = git_text(repo, "write-tree", env=ienv)
            if tree == parent_tree:
                print("note: feature %s produced no tree change; no commit" % feat["id"], file=sys.stderr)
                continue
            message = feat["title"].strip() + "\n\n" + feat["body"].strip() + "\n"
            if trailer:
                message += "\n" + trailer + "\n"
            commit = git_text(repo, "commit-tree", tree, "-p", parent, env=env, data=message.encode("utf-8"))
            commits.append((feat["id"], commit, len(paths)))
            parent, parent_tree = commit, tree

        # final check: tree == source minus excludes
        eidx = os.path.join(tmp, "expected")
        eenv = dict(env, GIT_INDEX_FILE=eidx)
        git(repo, "read-tree", source, env=eenv)
        drop = [(p, None) for p in src_tree if p not in public]
        if drop:
            git(repo, "update-index", "-z", "--index-info", env=eenv, data=index_info(drop))
        expected_tree = git_text(repo, "write-tree", env=eenv)

    if parent_tree != expected_tree:
        raise BuildError("final tree %s differs from source-minus-excludes %s; branch NOT created" % (parent_tree, expected_tree))

    git(repo, "update-ref", "-m", "build_public_history", ref, parent)
    report["branch"] = args.branch
    report["head"] = parent
    report["tree"] = parent_tree
    report["date"] = when
    report["co_author_trailer"] = trailer
    report["commits"] = [{"feature": f, "commit": c, "files": n} for f, c, n in commits]
    _emit(report, args.report)
    return 0


def _emit(report, path):
    text = json.dumps(report, indent=2)
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    print(text)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BuildError as exc:
        print("error: %s" % exc, file=sys.stderr)
        sys.exit(1)
