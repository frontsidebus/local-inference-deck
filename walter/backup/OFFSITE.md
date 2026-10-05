# Offsite backups (restic to S3)

`spark-offsite.timer` copies `${MODELS_DIR}/backups` (the local snapshots made by
`spark-backup.timer`) to an S3 bucket with [restic](https://restic.net). The restic repository
is encrypted client-side (AES-256 + Poly1305) with a password that exists only on Walter in
`/etc/spark-restic/password` and in the owner's offline copy. **Without that password the offsite
backups cannot be recovered by anyone, including AWS and the owner.**

The local snapshots hold every secret of the stack (DB dumps, `.env` files, keys), unencrypted.
Offsite they are protected by the restic encryption, the bucket being private, and the IAM user
being scoped to this one bucket.

| piece | where |
|---|---|
| bucket | `${RESTIC_BUCKET}` (site.env), region `${RESTIC_REGION}`; Block Public Access on, SSE-S3, versioning on, noncurrent versions expire after 30 days, incomplete multipart uploads abort after 7 days, tag `purpose=spark-backups` |
| repository | `s3:s3.<region>.amazonaws.com/<bucket>/walter` |
| IAM user | `spark-restic`: no console login, one access key, inline policy `spark-restic-bucket` ([template](offsite-iam-policy.json.tmpl)) |
| on Walter | `/usr/local/bin/restic` (pinned release, sha256-verified), `/etc/spark-restic/{aws.env,restic.env,password}` (root 0600, dir 0700), `/usr/local/sbin/spark-offsite.sh`, `spark-offsite.{service,timer}` |
| schedule | daily 04:30 UTC (+ up to 10 min), `Persistent=true`, `After=spark-backup.service`; waits for the local backup's lock if it is still running |
| retention | `restic forget --prune --keep-daily 14 --keep-weekly 8 --keep-monthly 6` (snapshots tagged `spark-backup`, host `walter`) |
| verification | `restic check --read-data-subset=25%` every Sunday (UTC), or `spark-offsite.sh --check` |
| status | journal (`journalctl -u spark-offsite`), `/var/lib/spark-offsite/LAST_OK`, `/var/lib/spark-offsite/spark_offsite.prom` (textfile format, not scraped yet) |

restic is the upstream release binary rather than Ubuntu's package: 24.04 ships 0.16.4, and the
pinned 0.19.1 has the exit code 10 ("repository does not exist") that `offsite-setup.sh` relies on
to never `init` over an existing repo it cannot read. The pin and its sha256 are in
`offsite-setup.sh`. The sha256 comes from the release's `SHA256SUMS`, whose GPG signature (key
`CF8F18F2844575973F79D4E191A6868BD3F7A907`) was checked when the pin was set.

## 1. AWS setup (one time, from an admin workstation)

Needs an AWS CLI with IAM and S3 admin rights. Never paste the access key into a terminal that
logs, and never write it to the workstation's disk: the pipeline below sends it straight to Walter.

```bash
export AWS_DEFAULT_REGION=us-east-1
BUCKET=spark-backups-$(openssl rand -hex 4)     # record it: RESTIC_BUCKET=$BUCKET in site.env
WALTER=${BACKEND_SSH_USER}@${BACKEND_LAN_IP}

# Bucket. Outside us-east-1 add: --create-bucket-configuration LocationConstraint=$AWS_DEFAULT_REGION
aws s3api create-bucket --bucket "$BUCKET" --object-ownership BucketOwnerEnforced
aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-encryption --bucket "$BUCKET" --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'
aws s3api put-bucket-versioning --bucket "$BUCKET" --versioning-configuration Status=Enabled
aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --lifecycle-configuration '{"Rules":[
  {"ID":"noncurrent-30d-abort-mpu-7d","Status":"Enabled","Filter":{},
   "NoncurrentVersionExpiration":{"NoncurrentDays":30},"AbortIncompleteMultipartUpload":{"DaysAfterInitiation":7}},
  {"ID":"expired-delete-markers","Status":"Enabled","Filter":{},"Expiration":{"ExpiredObjectDeleteMarker":true}}]}'
aws s3api put-bucket-tagging --bucket "$BUCKET" --tagging 'TagSet=[{Key=purpose,Value=spark-backups}]'

# IAM user: no login profile (no console), inline policy scoped to the bucket.
aws iam create-user --user-name spark-restic --tags Key=purpose,Value=spark-backups
aws iam put-user-policy --user-name spark-restic --policy-name spark-restic-bucket \
  --policy-document "$(RESTIC_BUCKET=$BUCKET envsubst '${RESTIC_BUCKET}' < walter/backup/offsite-iam-policy.json.tmpl)"

# Access key -> Walter /etc/spark-restic/aws.env (root 0600). Never printed, never on local disk.
ssh "$WALTER" 'sudo install -d -m 0700 -o root -g root /etc/spark-restic'
aws iam create-access-key --user-name spark-restic --query 'AccessKey.[AccessKeyId,SecretAccessKey]' --output text \
  | awk -v r="$AWS_DEFAULT_REGION" '{printf "AWS_ACCESS_KEY_ID=%s\nAWS_SECRET_ACCESS_KEY=%s\nAWS_DEFAULT_REGION=%s\n",$1,$2,r}' \
  | ssh "$WALTER" 'sudo sh -c "umask 077; cat > /etc/spark-restic/aws.env"'
```

Check the scoping (on Walter, with the restic key; credentials go to curl on stdin). A list of any
other bucket must return 403, the restic bucket 200:

```bash
sudo bash -c 'set -a; . /etc/spark-restic/aws.env; set +a
  for b in <some-other-bucket> <restic-bucket>; do
    printf "user = \"%s:%s\"\n" "$AWS_ACCESS_KEY_ID" "$AWS_SECRET_ACCESS_KEY" \
      | curl -s -o /dev/null -w "$b %{http_code}\n" -K - --aws-sigv4 "aws:amz:$AWS_DEFAULT_REGION:s3" \
        "https://$b.s3.$AWS_DEFAULT_REGION.amazonaws.com/?list-type=2&max-keys=1"
  done'
```

## 2. Walter setup

Set `RESTIC_BUCKET` and `RESTIC_REGION` in `site.env`, then on Walter from a checkout:

```bash
sudo walter/backup/offsite-setup.sh --dry-run
sudo walter/backup/offsite-setup.sh          # restic binary, password (generated once), restic.env, units, restic init, timer
sudo cat /etc/spark-restic/password          # OWNER: copy this OFFLINE now (password manager / paper), with the bucket name
sudo systemctl start spark-offsite && journalctl -u spark-offsite -n 30
```

`walter/deploy.sh` runs the same script when `RESTIC_BUCKET` is set and `/etc/spark-restic/aws.env`
exists. It never overwrites the password and never runs `init` against a repo it cannot read.

**Known issue (offsite off):** `RESTORE.md.tmpl` uses `${RESTIC_BUCKET}`, and `render.sh` refuses to render
a template whose site variable is empty. With `RESTIC_BUCKET=""` `walter/deploy.sh` therefore stops at
"render templates". Until that is fixed, use `RESTIC_BUCKET=CHANGEME` to keep offsite off: deploy treats
`CHANGEME` as off and the render passes.

**Owner action, once:** keep the restic password offline (password manager or paper) together with the bucket
name and region. It is the only way to read the offsite repository if Walter is lost; see `RESTORE.md` section 7.

## 3. Day-to-day

```bash
systemctl list-timers 'spark-*'
cat /var/lib/spark-offsite/LAST_OK
sudo bash -c 'set -a; . /etc/spark-restic/aws.env; . /etc/spark-restic/restic.env; set +a; restic snapshots'
sudo /usr/local/sbin/spark-offsite.sh --check      # run + full check of a 25% sample now
```

Restore steps (on Walter and on a fresh machine) are in `RESTORE.md` section 7.

**Rotate the access key:** `aws iam create-access-key` (pipe to Walter as in section 1; a user may
hold two keys), run `sudo systemctl start spark-offsite` to prove the new key works, then
`aws iam delete-access-key --user-name spark-restic --access-key-id <old id>`
(`aws iam list-access-keys --user-name spark-restic` shows the ids).

**Change the repo password:** `restic key add` (new password from a file), update
`/etc/spark-restic/password`, then `restic key list` and `restic key remove <old id>`. Update the
offline copy first.

## 4. Threat notes

- The IAM user can delete objects (restic prune needs it). Root on Walter can therefore delete the
  current objects, but versioning keeps every overwritten or deleted object for 30 days. Recover with
  admin credentials: `aws s3api list-object-versions --bucket "$BUCKET" --prefix walter/`, then
  remove the delete markers (`aws s3api delete-object --bucket "$BUCKET" --key <key> --version-id <marker id>`).
  Notice within 30 days: watch `LAST_OK` / the metric.
- The password and the AWS key are not under `${MODELS_DIR}/backups` and are not in the local
  backup's config tarball, so they are never inside the repo they protect.

## 5. Cost

S3 Standard in us-east-1: $0.023 per GB-month, PUT $0.005 per 1000, GET $0.0004 per 1000, upload
free, download $0.09 per GB (only when restoring). The local backups are about 5 MB per snapshot
(13 MB for three), so the repo plus 30 days of noncurrent versions stays well under 1 GB:
storage under $0.02 and a few thousand requests about $0.02 per month. Each extra GB kept costs
about $0.023 per month.

## 6. Remove it

```bash
# Walter
sudo systemctl disable --now spark-offsite.timer
sudo rm -f /etc/systemd/system/spark-offsite.{service,timer} /usr/local/sbin/spark-offsite.sh
sudo systemctl daemon-reload
sudo rm -rf /var/lib/spark-offsite /var/cache/restic /usr/local/bin/restic
sudo rm -rf /etc/spark-restic          # LAST: the password; only once the repo is no longer wanted
# AWS (admin workstation)
for k in $(aws iam list-access-keys --user-name spark-restic --query 'AccessKeyMetadata[].AccessKeyId' --output text); do
  aws iam delete-access-key --user-name spark-restic --access-key-id "$k"; done
aws iam delete-user-policy --user-name spark-restic --policy-name spark-restic-bucket
aws iam delete-user --user-name spark-restic
# A versioned bucket must be emptied of all versions and delete markers first:
python3 - "$BUCKET" <<'EOF'
import sys, boto3
boto3.resource("s3").Bucket(sys.argv[1]).object_versions.delete()
EOF
aws s3api delete-bucket --bucket "$BUCKET"
```

Without boto3, page through `aws s3api list-object-versions` and pass `Versions` and
`DeleteMarkers` to `aws s3api delete-objects`, or empty the bucket in the S3 console.
