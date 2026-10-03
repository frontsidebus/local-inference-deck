#!/usr/bin/env bash
# clients/deploy.sh -- per-component entry point (see CONVENTIONS.md).
# The client side is installed per user on each workstation; this just runs install.sh.
set -euo pipefail
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/install.sh" "$@"
