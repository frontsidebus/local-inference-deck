# deck-tools

The shared engines of local-inference-deck, vendored by the repos that use them (agent-judge, sec-evals).
Origin: `tools/deck-tools/` in local-inference-deck. Releases are tags named `deck-tools-v<VERSION>`.
Everything is bash or stdlib python3.

| File | What it does |
|---|---|
| `render.sh` | renders `*.tmpl` with site values (envsubst with an explicit variable list, `@@if NAME@@` blocks) |
| `gen-secrets.sh` | generates a component's secrets on the target host from `<spec-dir>/<component>.sh`; never overwrites or prints one |
| `check-sanitized.sh` | fails when real site values, listed names or secret-shaped strings are in tracked files or in history |
| `deck-tools` | `verify`, `lock`, `sync`, `install-hooks`, `version` (below) |
| `pre-commit` | the pre-commit hook: `deck-tools verify --index`, then check-sanitized on the staged files |
| `VERSION`, `CHANGELOG.md`, `README.md` | the release |

## Engine CLI

Each engine's header has the full usage. A repo calls the engines through executable two-line shims in
`scripts/`, which keep the old paths working:

```bash
#!/usr/bin/env bash
here=$(dirname "${BASH_SOURCE[0]}")
exec "$here/../tools/deck-tools/render.sh" --repo "$here/.." "$@"
```

- `render.sh [--repo DIR] [--keys FILE] [-e SITE_ENV] [-x GLOB]... SRC DST` (also `--vars` and `--check SRC`).
  The variables replaced are the names in `--keys FILE` (default `<repo>/site.env.example`) plus the names in
  the site env, so a name that is only in the site env still expands. Site env: `-e`, else `$SITE_ENV`, else
  `~/.config/deck/site.env`, else `<repo>/site.env`.
- `gen-secrets.sh <component> [--dry-run] [--destdir DIR] [--site-env FILE] [--repo DIR] [--spec-dir DIR]`.
  The spec is `<spec-dir>/<component>.sh` (default `<repo>/scripts/secrets.d`). It is sourced with `REPO`
  set to `--repo`.
- `check-sanitized.sh [--all | --history] [--regex-only] [--site-env FILE] [--repo DIR] [--keys FILE]`.
  - The repo checked is `--repo`, else the git work tree of the current directory. The check-sanitized shim
    passes no `--repo`, so `cd <other checkout> && <deck>/scripts/check-sanitized.sh` checks that checkout.
  - Example baseline: `--keys FILE` (a registry if it ends in `.yaml`/`.yml`, else an env-style example), then
    `<repo>/contracts/site-keys.yaml`, then the union of `<repo>/*.env.example`. A site value equal to its
    example default is public. With no baseline every site value counts as real.
  - Policy: `<repo>/contracts/sanitize-policy.yaml`, else the built-in table.
  - Exit codes: 0 clean, 1 finding, 2 no real-value source, a dangling symlink or a refused invocation.
    `--regex-only` is refused unless `CI=true`.

Options can come in any order; when one is repeated, the last value wins. So a caller can pass its own
`--repo` after the shim's.

## The lock: `tools/tools.lock`

A line-based file, so it is readable without a YAML parser:

```
# comments
lock_format: 1
version: 1.0.0
source: origin                       # or: local-inference-deck deck-tools-v1.0.0 <commit>  (written by sync)
files:
100755 <sha256> tools/deck-tools/check-sanitized.sh
100644 <sha256> tools/deck-tools/README.md
...
```

There is one line per file under `tools/deck-tools/`, sorted by path. The mode is git's: `100755` is
executable and `100644` is not.

The executable bit matters. For example, `covenant/deploy.sh` runs `scripts/gen-secrets.sh` only when it is
executable, and otherwise sources the spec directly.

## Commands

- `deck-tools verify [--repo DIR] [--index] [--base REV] [--quiet]` fails (exit 1) when:
  - a listed file's content or mode differs, a file is missing, or a file under `tools/deck-tools/` is not
    listed;
  - git records a different mode for a tracked file;
  - `VERSION` differs from the lock's `version`, or `CHANGELOG.md` has no `## <version>` entry.

  The options:
  - `--index` checks what is staged (the hook uses it).
  - `--base REV` also fails when the files changed since the lock at REV but the version did not go up.
  - Exit 2 means there is no lock, or the lock is invalid.
- `deck-tools lock [--repo DIR]` runs in local-inference-deck only, and writes `source: origin`. In a
  vendored copy it refuses: change the origin and sync instead.
- `deck-tools sync --from DIR [--repo DIR] [--allow-dirty]` copies a release into this repo and rewrites its
  lock. DIR is a local-inference-deck checkout at a `deck-tools-v*` tag.
  - DIR must pass its own `verify`, and have no uncommitted changes under `tools/` (unless `--allow-dirty`).
  - Files that the release no longer has are removed.
  - The lock's `source` names the tag and commit, never a local path.
- `deck-tools install-hooks [--repo DIR] [--force]` installs a stub hook in the clone's common hooks dir. The
  stub runs the committing work tree's `hooks/pre-commit`, or else `tools/deck-tools/pre-commit`, so it serves
  every worktree.
  - With `core.hooksPath=hooks` already set, it reports that the hook is wired and changes nothing.
  - A different existing hook is kept, unless you pass `--force`; it is then saved as
    `pre-commit.pre-deck-tools`.
- `deck-tools version` prints the locked version.

## Vendoring deck-tools into a consumer repo

1. In a clean local-inference-deck checkout, check out the release: `git checkout deck-tools-v1.0.0`.
2. In the consumer repo, copy it in:
   `python3 <deck>/tools/deck-tools/deck-tools sync --from <deck> --repo .`
3. Add the shims that the repo's callers use, for example `scripts/check-sanitized.sh`. agent-judge's C10
   contract needs that one to be executable. Then run `tools/deck-tools/deck-tools install-hooks`.
4. Add a CI step: `python3 tools/deck-tools/deck-tools verify`, plus `--base origin/<base>` on pull requests.
5. Commit `tools/deck-tools/` and `tools/tools.lock` together. To upgrade later, repeat with the newer tag.

The consumer keeps its own example baseline and policy (its `*.env.example`, or a `contracts/site-keys.yaml`).
Adding a deck key never needs a deck-tools release.

## Changing an engine (in local-inference-deck)

1. Edit the file under `tools/deck-tools/`.
2. Bump `VERSION` (patch for a fix, minor for a new option, major for a changed exit code or a removed option)
   and add the `CHANGELOG.md` entry.
3. Run `python3 tools/deck-tools/deck-tools lock`.
4. Commit all of it together. The pre-commit hook (`verify --index`) blocks a commit whose staged files do not
   match the staged lock. CI runs `verify` (and `--base` on pull requests), so a forgotten lock or version bump
   fails before merge.
5. After the owner merges, the lead tags the merge commit `deck-tools-v<VERSION>`.
