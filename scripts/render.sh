#!/usr/bin/env bash
# Shim: the engine is tools/deck-tools/render.sh (vendored deck-tools; see tools/deck-tools/README.md).
here=$(dirname "${BASH_SOURCE[0]}")
exec "$here/../tools/deck-tools/render.sh" --repo "$here/.." "$@"
