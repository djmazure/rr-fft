# Public source mirror (FFT-P3.3)

The development repo is private and keeps its full history. That history
predates the genericization pass in v0.1.1, so it is never published. The
public source home is a separate repo with a fresh history built by
`tools/public_mirror.py`.

## What the mirror contains

| Item | Content |
|---|---|
| Commits | One per released tag, in release order. Tree identical to the private tag's tree (same tree hash). |
| Tags | Re-created as annotated tags on the mirror commits. |
| Identity and dates | Copied from the private release commit. A rebuild with one more `--release` reproduces the earlier commits, so the push is a fast-forward. |
| Private history | Not reachable and not in the object store (pruned before the scan). |
| First release | v0.1.1. v0.1.0 predates the genericization pass and is refused by the gate. |

## Sanity gate

Before `build` reports success and before `push` sends anything, every
commit reachable from any ref is scanned for the consumer terms: tree,
message, author and committer, plus annotated tag messages and ref names.
A hit is reported by term index, not by text. Exit code 1 means REFUSED.

The terms come from a private file passed with `--terms-file`, one term per
line, `#` for comments. It never lives in this repo, because a term list in
the public tree would publish the terms. An empty list is refused (exit
code 2).

## Procedure

| Step | Command |
|---|---|
| 1. Build | `python tools/public_mirror.py build --source . --release v0.1.1 --release v0.2.0 --out <new dir> --terms-file <private file>` |
| 2. Check (optional, any repo) | `python tools/public_mirror.py check --repo <repo> --terms-file <private file>` |
| 3. Push | `python tools/public_mirror.py push --repo <new dir> --remote <public url> --terms-file <private file>` |
| 4. Registry | Publish later versions from the mirror so the registry pins mirror commits. Re-run the registry verify runner against the mirror. |

`push` does not force. A remote whose existing history differs from the
mirror refuses the push; replacing that remote is a manual decision.
