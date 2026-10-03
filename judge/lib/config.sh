#!/usr/bin/env bash
# judge/lib/config.sh: bash access to the judge config (site.env + judge defaults).
#
# Source it, then call judge_load_config:
#   . "$(dirname "$0")/../lib/config.sh"; judge_load_config
# Exports every key from judge/lib/config.py (defaults < site.env < environment), including the resolved
# HERMES_HOME and JUDGE_REVIEW_DIR. site.env is parsed by config.py, never sourced, so it cannot run code.
#
#   judge_load_config         export all config keys into the current shell
#   judge_get KEY             print one value
#   judge_review_dir          print the review dir
#   judge_host_ssh HOST       print the ssh argv prefix for walter|covenant (shell-quoted)

JUDGE_LIB_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

judge_load_config() {
  local out
  out=$(python3 "$JUDGE_LIB_DIR/config.py" --export) || return 1
  eval "$out"
}

judge_get() { python3 "$JUDGE_LIB_DIR/config.py" --get "$1"; }
judge_review_dir() { python3 "$JUDGE_LIB_DIR/config.py" --review-dir; }
judge_host_ssh() { python3 "$JUDGE_LIB_DIR/config.py" --ssh "$1"; }
