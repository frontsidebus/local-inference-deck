#!/usr/bin/env bash
# Shim: the engine is tools/deck-tools/check-sanitized.sh (vendored deck-tools; see tools/deck-tools/README.md).
# No --repo here, on purpose: the repo checked is the git work tree of the current directory, as before, so
# `cd <other checkout> && <deck>/scripts/check-sanitized.sh` (the judge's check_sanitized probe) checks that one.
here=$(dirname "${BASH_SOURCE[0]}")
exec "$here/../tools/deck-tools/check-sanitized.sh" "$@"
