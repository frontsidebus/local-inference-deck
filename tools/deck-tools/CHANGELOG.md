# deck-tools changelog

Every change to a file under `tools/deck-tools/` bumps `VERSION` (semver) and adds an entry here, in the same
commit as `deck-tools lock`. `deck-tools verify` requires an entry for the locked version.

## 1.0.0

First release, cut from local-inference-deck (repo-split step 1, three-repo variant). Tag: `deck-tools-v1.0.0`.

- The engines move from `scripts/` to `tools/deck-tools/`: `render.sh`, `gen-secrets.sh`, `check-sanitized.sh`.
  `scripts/{render,gen-secrets,check-sanitized}.sh` stay as executable shims, so callers are unchanged.
- An explicit repo root: `--repo DIR` on all three. `render.sh --keys FILE` (default `<repo>/site.env.example`)
  keeps the union semantics: the variable set is the names in FILE plus the names in the site env.
  `gen-secrets.sh --spec-dir DIR` (default `<repo>/scripts/secrets.d`).
- `check-sanitized.sh` reads its example baseline from the repo it checks: `--keys FILE` >
  `contracts/site-keys.yaml` > the union of `*.env.example` at the root. With none, every site value counts as
  real. Exit codes stay 0 clean, 1 finding, 2 no real-value source or a dangling symlink; `--regex-only` only
  with `CI=true`.
- `deck-tools verify`, `lock`, `sync --from DIR`, `install-hooks`, and `tools/tools.lock` (sha256 and mode of
  every file here). `pre-commit` runs `verify --index` and then check-sanitized.
