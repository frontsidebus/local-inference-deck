#!/usr/bin/env bash
# walter/backup/offsite-setup.sh -- set up restic offsite backups on Walter. Idempotent.
#
# Usage (on Walter, from a checkout of this repo, as root):
#   sudo walter/backup/offsite-setup.sh [--site-env FILE] [--dry-run] [--no-start]
#
# Prerequisites (one-time, see walter/backup/OFFSITE.md):
#   - RESTIC_BUCKET / RESTIC_REGION in site.env (bucket created by the AWS runbook)
#   - /etc/spark-restic/aws.env (root 0600) with the bucket-scoped IAM user's key:
#       AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_DEFAULT_REGION=...
#
# Steps: restic binary (pinned, sha256-verified) -> /etc/spark-restic/password (generated once,
# never printed, never overwritten) -> restic.env -> spark-offsite.sh + units -> restic init
# (only if the repo does not exist) -> enable spark-offsite.timer.
# Contains no secrets. Never deletes anything.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)

# Pinned: official release binary, checked against the sha256 below. That value is from the
# release's SHA256SUMS, whose GPG signature (restic key CF8F18F2844575973F79D4E191A6868BD3F7A907)
# was verified when the pin was set. Ubuntu 24.04 ships only 0.16.4.
RESTIC_VERSION=0.19.1
RESTIC_URL="https://github.com/restic/restic/releases/download/v${RESTIC_VERSION}/restic_${RESTIC_VERSION}_linux_amd64.bz2"
RESTIC_SHA256=f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c   # the .bz2 download
CONF=/etc/spark-restic

DRY=0 NO_START=0 SITE_ENV_FILE="$REPO/site.env"
while [[ $# -gt 0 ]]; do
  case $1 in
    --dry-run) DRY=1 ;;
    --no-start) NO_START=1 ;;
    --site-env) SITE_ENV_FILE=$(realpath "${2:?}"); shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "offsite-setup: unknown option $1" >&2; exit 2 ;;
  esac
  shift
done

say()  { printf '%s\n' "$*"; }
die()  { printf 'offsite-setup: %s\n' "$*" >&2; exit 1; }
run()  { if [[ $DRY == 1 ]]; then say "  would run: $*"; else "$@"; fi; }

[[ -f $SITE_ENV_FILE ]] || die "site env not found: $SITE_ENV_FILE"
[[ $DRY == 1 || $EUID -eq 0 ]] || die "run as root (or --dry-run)"
set -a; . "$SITE_ENV_FILE"; set +a
[[ -n ${RESTIC_BUCKET:-} && $RESTIC_BUCKET != CHANGEME ]] || die "RESTIC_BUCKET is not set in $SITE_ENV_FILE"
RESTIC_REGION=${RESTIC_REGION:-us-east-1}
[[ -n ${MODELS_DIR:-} ]] || die "MODELS_DIR is empty"
REPO_URL="s3:s3.${RESTIC_REGION}.amazonaws.com/${RESTIC_BUCKET}/walter"

# ---- restic binary ----------------------------------------------------------------------
say "== restic $RESTIC_VERSION"
if [[ -x /usr/local/bin/restic ]] && /usr/local/bin/restic version 2>/dev/null | grep -q "restic $RESTIC_VERSION "; then
  say "  /usr/local/bin/restic $RESTIC_VERSION present"
elif [[ $DRY == 1 ]]; then
  say "  would download $RESTIC_URL and verify sha256 $RESTIC_SHA256"
else
  tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
  curl -fsSL -o "$tmp/restic.bz2" "$RESTIC_URL"
  echo "$RESTIC_SHA256  $tmp/restic.bz2" | sha256sum -c --quiet || die "restic sha256 mismatch"
  bunzip2 "$tmp/restic.bz2"
  install -m 0755 -o root -g root "$tmp/restic" /usr/local/bin/restic
  say "  installed $(/usr/local/bin/restic version)"
fi

# ---- /etc/spark-restic --------------------------------------------------------------------
say "== $CONF"
run install -d -m 0700 -o root -g root "$CONF"
if [[ -s $CONF/password ]]; then say "  password present (kept)"
elif [[ $DRY == 1 ]]; then say "  would generate $CONF/password (0600)"
else
  (umask 077; openssl rand -hex 24 > "$CONF/password.new" && mv "$CONF/password.new" "$CONF/password")
  say "  generated $CONF/password (0600). Make an OFFLINE copy now: sudo cat $CONF/password"
fi
want_env=$(printf 'RESTIC_REPOSITORY=%s\nRESTIC_PASSWORD_FILE=%s\n' "$REPO_URL" "$CONF/password")
if [[ -f $CONF/restic.env ]] && [[ $(cat "$CONF/restic.env") == "$want_env" ]]; then say "  restic.env up to date"
elif [[ $DRY == 1 ]]; then say "  would write $CONF/restic.env (RESTIC_REPOSITORY=$REPO_URL)"
else (umask 077; printf '%s\n' "$want_env" > "$CONF/restic.env"); say "  wrote $CONF/restic.env"
fi
[[ -s $CONF/aws.env || $DRY == 1 ]] || die "$CONF/aws.env is missing: create the IAM key first (walter/backup/OFFSITE.md)"

# ---- script + units -----------------------------------------------------------------------
say "== spark-offsite script and units"
STAGE=$(mktemp -d); trap 'rm -rf "$STAGE" ${tmp:+"$tmp"}' EXIT
"$REPO/scripts/render.sh" -e "$SITE_ENV_FILE" "$HERE" "$STAGE/backup" >/dev/null
changed=0
inst() { # SRC DEST MODE
  if [[ -f $2 ]] && cmp -s "$1" "$2"; then return 0; fi
  say "  install  $2"; changed=1
  [[ $DRY == 1 ]] || install -m "$3" -o root -g root "$1" "$2"
}
inst "$STAGE/backup/spark-offsite.sh"      /usr/local/sbin/spark-offsite.sh         0750
inst "$STAGE/backup/spark-offsite.service" /etc/systemd/system/spark-offsite.service 0644
inst "$STAGE/backup/spark-offsite.timer"   /etc/systemd/system/spark-offsite.timer   0644
inst "$STAGE/backup/RESTORE.md"            "$MODELS_DIR/backups/RESTORE.md"          0600
[[ $changed == 1 ]] && run systemctl daemon-reload

# ---- repository ---------------------------------------------------------------------------
say "== restic repository $REPO_URL"
if [[ $DRY == 1 ]]; then say "  would check the repo and run 'restic init' if it does not exist"
else
  set -a; . "$CONF/aws.env"; . "$CONF/restic.env"; set +a
  rc=0; /usr/local/bin/restic cat config >/dev/null 2>"$STAGE/cat.err" || rc=$?
  case $rc in
    0)  say "  repository exists" ;;
    10) /usr/local/bin/restic init ;;    # exit 10 = repository does not exist (restic >= 0.17)
    *)  sed 's/^/  /' "$STAGE/cat.err" >&2
        die "repository not readable (rc=$rc: wrong password, key or network?); not running init" ;;
  esac
fi

# ---- timer --------------------------------------------------------------------------------
run systemctl enable spark-offsite.timer
[[ $NO_START == 1 ]] || run systemctl start spark-offsite.timer
say "done. First run now: sudo systemctl start spark-offsite; logs: journalctl -u spark-offsite"
