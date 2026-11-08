# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MIT
"""Regression guard for FFT-P3.3, asserted at the tools/public_mirror.py CLI.

The public source home must never carry the private history, whose old
commits and messages name the consumer architecture while the current tree
is clean. Every test builds its own fixture repos under tmp_path, uses a
synthetic term, and pushes only to a local bare repo: nothing here touches
a network remote or the real terms list.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "public_mirror.py"

TERM = "acme-widget"
IDENT = {
    "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.com",
    "GIT_COMMITTER_NAME": "Fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.com",
    "GIT_AUTHOR_DATE": "2026-07-01T12:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-07-01T12:00:00+00:00",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


def hermetic_env() -> dict:
    """os.environ without any GIT_* variable, plus the fixture identity.

    Run from a pre-commit hook (the rr-fft commit gate runs this file),
    git exports GIT_INDEX_FILE and friends for the OUTER repository. A
    fixture `git -C <tmp repo> add` that inherits them writes into the
    outer index, so the commit under test would carry a fixture file."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(IDENT)
    return env


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        text=True, env=hermetic_env()).stdout


def probe(repo: Path, *args: str) -> str | None:
    """git output, or None when the repo or object is missing."""
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True, env=hermetic_env())
    return r.stdout.strip() if r.returncode == 0 else None


def commit(repo: Path, readme: str, message: str) -> str:
    (repo / "README.md").write_text(readme)
    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


def make_source(tmp: Path, old_readme: str, old_msg: str) -> tuple[Path, str]:
    """Two commits: an old one (possibly naming TERM), then a clean v1."""
    src = tmp / "src"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    old = commit(src, old_readme, old_msg)
    commit(src, "Generic streaming FFT.\n", "docs: genericize")
    git(src, "tag", "-a", "v1", "-m", "v1")
    return src, old


def terms_file(tmp: Path, *lines: str) -> Path:
    path = tmp / "terms.txt"
    path.write_text("".join(f"{line}\n" for line in lines))
    return path


def tool(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(TOOL), *args],
                          capture_output=True, text=True, env=hermetic_env())


def test_check_refuses_term_only_in_old_file_version(tmp_path):
    src, _ = make_source(tmp_path, f"First target: the {TERM} datapath.\n",
                         "docs: first draft")
    r = tool("check", "--repo", str(src),
             "--terms-file", str(terms_file(tmp_path, TERM)))
    got = (r.returncode, "README.md" in r.stdout)
    assert got == (1, True), (
        f"history-only term in an old file version not refused: {r}")


def test_check_refuses_term_only_in_commit_message(tmp_path):
    src, _ = make_source(tmp_path, "Draft.\n", f"docs: as used by {TERM}")
    r = tool("check", "--repo", str(src),
             "--terms-file", str(terms_file(tmp_path, TERM)))
    got = (r.returncode, "message/identity" in r.stdout)
    assert got == (1, True), f"term only in a commit message not refused: {r}"


def test_build_mirror_is_fresh_history(tmp_path):
    # The old commit is term-free, so only the freshness of the mirror's
    # history (not the term scan) decides what this test sees.
    src, old = make_source(tmp_path, "Draft.\n", "docs: first draft")
    out = tmp_path / "mirror.git"
    r = tool("build", "--source", str(src), "--release", "v1",
             "--out", str(out), "--terms-file", str(terms_file(tmp_path, TERM)))
    revs = probe(out, "rev-list", "--all")
    commits = None if revs is None else len(revs.split())
    same_tree = (probe(out, "rev-parse", "v1^{tree}")
                 == git(src, "rev-parse", "v1^{tree}").strip())
    old_present = probe(out, "cat-file", "-e", old) is not None
    got = (r.returncode, commits, same_tree, old_present)
    assert got == (0, 1, True, False), (
        f"mirror is not a fresh one-commit history of the release tree: "
        f"{got} {r}")


def test_build_refuses_release_that_names_term(tmp_path):
    src, old = make_source(tmp_path, f"For {TERM}.\n", "docs: first draft")
    git(src, "tag", "-a", "v0", "-m", "v0", old)
    r = tool("build", "--source", str(src), "--release", "v0",
             "--out", str(tmp_path / "m.git"),
             "--terms-file", str(terms_file(tmp_path, TERM)))
    assert (r.returncode, "REFUSED" in r.stdout) == (1, True), (
        f"release tree naming a consumer term not refused: {r}")


def test_push_refuses_dirty_repo_and_pushes_nothing(tmp_path):
    src, _ = make_source(tmp_path, "Draft.\n", "docs: first draft")
    commit(src, f"For {TERM}.\n", "docs: deployment note")
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    r = tool("push", "--repo", str(src), "--remote", str(remote),
             "--terms-file", str(terms_file(tmp_path, TERM)))
    pushed = git(remote, "for-each-ref", "--format=%(refname)").split()
    assert (r.returncode, pushed) == (1, []), (
        f"push of a repo with a consumer term in its history not refused: "
        f"{pushed} {r}")


def test_push_clean_mirror_to_local_remote(tmp_path):
    src, _ = make_source(tmp_path, "Draft.\n", "docs: first draft")
    terms = str(terms_file(tmp_path, TERM))
    out = tmp_path / "mirror.git"
    assert tool("build", "--source", str(src), "--release", "v1",
                "--out", str(out), "--terms-file", terms).returncode == 0
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    r = tool("push", "--repo", str(out), "--remote", str(remote),
             "--terms-file", terms)
    pushed = sorted(git(remote, "for-each-ref", "--format=%(refname)").split())
    assert (r.returncode, pushed) == (0, ["refs/heads/main", "refs/tags/v1"]), (
        f"clean mirror not pushed with its tags: {pushed} {r}")


def test_empty_terms_file_is_refused(tmp_path):
    src, _ = make_source(tmp_path, f"For {TERM}.\n", "docs: first draft")
    r = tool("check", "--repo", str(src),
             "--terms-file", str(terms_file(tmp_path, "# no terms yet")))
    assert (r.returncode, "lists no terms" in r.stderr) == (2, True), (
        f"empty terms file accepted, the gate would pass everything: {r}")


def test_rebuild_with_new_release_extends_mirror_fast_forward(tmp_path):
    # A later release is published by rebuilding with one more --release;
    # the push stays a fast-forward only if earlier commits are reproduced.
    src, _ = make_source(tmp_path, "Draft.\n", "docs: first draft")
    terms = str(terms_file(tmp_path, TERM))
    first, second = tmp_path / "a.git", tmp_path / "b.git"
    tool("build", "--source", str(src), "--release", "v1",
         "--out", str(first), "--terms-file", terms)
    commit(src, "Generic streaming FFT, v2.\n", "feat: v2")
    git(src, "tag", "-a", "v2", "-m", "v2")
    tool("build", "--source", str(src), "--release", "v1", "--release", "v2",
         "--out", str(second), "--terms-file", terms)
    got = (probe(first, "rev-parse", "main"),
           probe(second, "rev-parse", "main~1"),
           probe(first, "show", "-s", "--format=%cI %ae", "main"))
    want = (got[1] or "<no mirror>", got[1],
            "2026-07-01T12:00:00+00:00 fixture@example.com")
    assert got == want, f"rebuilt mirror does not extend the old one: {got}"
