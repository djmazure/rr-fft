# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MIT
"""Build, check and push the fresh-history public mirror of rr-fft (FFT-P3.3).

The private development repo keeps its full history, and that history
predates the 2026-07-17 genericization pass: old commit messages and old
file versions name the consumer architecture. A force-rewrite is ruled out
because the registry verify records pin its commit hashes. The public
source home is therefore a separate repo whose history is FRESH: one commit
per released tag, each carrying exactly the release's tree, tags re-created
on those commits. Nothing from the private history is reachable from it.

Sanity gate: a mirror is pushed only after every commit reachable from any
of its refs has been scanned, tree AND message AND identity, plus every
annotated tag message and ref name, for the consumer terms. Scanning only
the current tree is not enough: a tree can be clean while an older commit
still carries the terms, and that is how a published history leaks.

The terms are read from a private file (one term per line, '#' comments)
that never lives in this repo, because a term list in the public tree would
publish the terms. An empty term list is refused: a gate with nothing to
look for passes everything.

    public_mirror.py build --source <repo> --release v0.1.1 --release v0.2.0 \\
        --out <new dir> --terms-file <private file>
    public_mirror.py check --repo <repo> --terms-file <private file>
    public_mirror.py push  --repo <mirror> --remote <url> --terms-file <file>

Exit codes: 0 clean / done, 1 consumer term found (REFUSED), 2 usage error.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_BRANCH = "main"


class UsageError(Exception):
    pass


def git(repo: Path, *args: str, env: dict | None = None,
        check: bool = True) -> str:
    full_env = None
    if env:
        full_env = dict(os.environ)
        full_env.update(env)
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        errors="replace", env=full_env)
    if check and proc.returncode != 0:
        raise UsageError(
            f"git {' '.join(args)} failed in {repo}: {proc.stderr.strip()}")
    return proc.stdout


def load_terms(path: Path) -> list[str]:
    if not path.is_file():
        raise UsageError(f"terms file not found: {path}")
    terms = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            terms.append(line)
    if not terms:
        raise UsageError(
            f"terms file {path} lists no terms; a gate with nothing to look "
            "for passes everything")
    return terms


def scan(repo: Path, terms: list[str]) -> list[str]:
    """Return one line per consumer-term hit anywhere in the repo's refs.

    Covers every commit reachable from any ref (tree, message, author and
    committer), every annotated tag message, and every ref name. A hit
    names the term by its index in the terms file, never by its text, so
    the report itself does not spread the term.
    """
    hits: list[str] = []
    lowered = [t.lower() for t in terms]

    def text_hits(where: str, text: str) -> None:
        low = text.lower()
        for i, term in enumerate(lowered):
            if term in low:
                hits.append(f"term #{i} in {where}")

    refs = git(repo, "for-each-ref", "--format=%(refname)%00%(objecttype)")
    for line in refs.splitlines():
        refname, objtype = line.split("\0")
        text_hits(f"ref name {refname}", refname)
        if objtype == "tag":
            text_hits(f"tag message {refname}",
                      git(repo, "cat-file", "tag", refname))

    commits = git(repo, "rev-list", "--all").split()
    for sha in commits:
        # The raw commit object holds the message and both identities.
        text_hits(f"commit {sha[:12]} message/identity",
                  git(repo, "cat-file", "commit", sha))
        for i, term in enumerate(terms):
            out = git(repo, "grep", "-a", "-i", "-F", "-l", "-e", term, sha,
                      check=False)
            for path in out.splitlines():
                hits.append(
                    f"term #{i} in commit {sha[:12]} file "
                    f"{path.split(':', 1)[-1]}")
    return hits


def ident_env(source: Path, sha: str) -> dict:
    fmt = "%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI"
    an, ae, ad, cn, ce, cd = git(
        source, "show", "-s", f"--format={fmt}", sha).strip().split("\0")
    return {
        "GIT_AUTHOR_NAME": an, "GIT_AUTHOR_EMAIL": ae, "GIT_AUTHOR_DATE": ad,
        "GIT_COMMITTER_NAME": cn, "GIT_COMMITTER_EMAIL": ce,
        "GIT_COMMITTER_DATE": cd,
    }


def build(source: Path, releases: list[str], out: Path,
          terms: list[str], branch: str = DEFAULT_BRANCH) -> list[str]:
    if not releases:
        raise UsageError("name at least one --release tag")
    if out.exists() and any(out.iterdir()):
        raise UsageError(f"output dir {out} exists and is not empty")
    out.mkdir(parents=True, exist_ok=True)
    git(out, "init", "-q", "--bare")

    parent = None
    for tag in releases:
        sha = git(source, "rev-parse", "--verify", f"{tag}^{{commit}}").strip()
        tree = git(source, "rev-parse", f"{sha}^{{tree}}").strip()
        # Only the tree's objects are wanted; the fetched history is
        # unreferenced again below and pruned before anything is scanned.
        git(out, "fetch", "-q", "--no-tags", str(source.resolve()),
            f"{sha}:refs/mirror-import/{tag}")
        env = ident_env(source, sha)
        msg = (f"rr-fft {tag}\n\nPublic source of the routertl/fft package "
               f"at {tag}. The development history is kept privately.\n")
        args = ["commit-tree", tree, "-m", msg]
        if parent:
            args[2:2] = ["-p", parent]
        parent = git(out, *args, env=env).strip()
        git(out, "tag", "-a", tag, "-m", f"rr-fft {tag}", parent, env=env)
    git(out, "update-ref", f"refs/heads/{branch}", parent)
    git(out, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
    for ref in git(out, "for-each-ref", "--format=%(refname)",
                   "refs/mirror-import").split():
        git(out, "update-ref", "-d", ref)
    git(out, "reflog", "expire", "--expire=now", "--all")
    git(out, "gc", "-q", "--prune=now")
    return scan(out, terms)


def push(repo: Path, remote: str, terms: list[str],
         branch: str = DEFAULT_BRANCH) -> list[str]:
    hits = scan(repo, terms)
    if hits:
        return hits
    tags = git(repo, "for-each-ref", "--format=%(refname)", "refs/tags").split()
    git(repo, "push", "-q", remote, f"refs/heads/{branch}", *tags)
    return []


def report(hits: list[str], what: str) -> int:
    if hits:
        print(f"REFUSED: {len(hits)} consumer-term hit(s) in {what}:")
        for h in hits:
            print(f"  {h}")
        return 1
    print(f"CLEAN: no consumer term in {what}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build a fresh-history mirror")
    b.add_argument("--source", type=Path, required=True)
    b.add_argument("--release", action="append", default=[])
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--branch", default=DEFAULT_BRANCH)
    c = sub.add_parser("check", help="scan a repo's tree and history")
    c.add_argument("--repo", type=Path, required=True)
    p = sub.add_parser("push", help="scan, then push branch and tags")
    p.add_argument("--repo", type=Path, required=True)
    p.add_argument("--remote", required=True)
    p.add_argument("--branch", default=DEFAULT_BRANCH)
    for sp in (b, c, p):
        sp.add_argument("--terms-file", type=Path, required=True)
    args = ap.parse_args(argv)

    try:
        terms = load_terms(args.terms_file)
        if args.cmd == "build":
            hits = build(args.source, args.release, args.out, terms,
                         args.branch)
            return report(hits, f"mirror {args.out}")
        if args.cmd == "check":
            return report(scan(args.repo, terms), f"repo {args.repo}")
        hits = push(args.repo, args.remote, terms, args.branch)
        if hits:
            return report(hits, f"repo {args.repo}; nothing pushed")
        print(f"PUSHED {args.branch} and tags to {args.remote}")
        return 0
    except UsageError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
