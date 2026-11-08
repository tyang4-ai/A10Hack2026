#!/usr/bin/env bash
# Redeploy the Axelrod API on EC2. Runs on the laptop, from the repo root, with workshop credentials.
#
#   bash backend/infra/deploy.sh                # ship HEAD and install it on the instance
#   bash backend/infra/deploy.sh --upload-only  # ship HEAD to S3 only (before the first launch)
#   bash backend/infra/deploy.sh --ref <commit> # ship another commit (a rollback, for example)
#
# 1. git archive of the commit (backend/ and contract/ only, so uncommitted edits never ship) plus a
#    DEPLOYED_SHA file, uploaded to s3://$VAULT_BUCKET/deploy/axelrod-<sha>.tar.gz and copied to
#    deploy/latest.tar.gz.
# 2. aws ssm send-command (AWS-RunShellScript) on $EC2_INSTANCE_ID runs infra/remote_deploy.sh:
#    download with the instance role, extract to /opt/axelrod (venv kept), reinstall
#    backend/requirements.txt, restart the axelrod service, print the deployed sha and /healthz.
#
# Settings: the real environment wins, then backend/.env (VAULT_BUCKET, EC2_INSTANCE_ID,
# AWS_ACCOUNT_ID, optional API_INVOKE_URL). No credentials are read or written here.
set -euo pipefail
export MSYS_NO_PATHCONV=1 AWS_PAGER=""
REGION=us-east-1

envval() {
  local k="$1" v="${!1:-}"
  if [ -z "$v" ] && [ -f backend/.env ]; then
    v="$(grep -E "^$k=" backend/.env | tail -n1 | cut -d= -f2- | tr -d '\r')" || true
    v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
  fi
  printf '%s' "$v"
}

# Git Bash: path conversion is off (for aws.exe), so temp paths are given as C:/... (cygpath -m).
mixedpath() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi; }

[ -d backend/infra ] && [ -d contract ] || { echo "run from the repo root" >&2; exit 2; }
upload_only=0
ref=HEAD
while [ $# -gt 0 ]; do
  case "$1" in
    --upload-only) upload_only=1; shift ;;
    --ref) ref="${2:?--ref needs a commit}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

want="$(envval AWS_ACCOUNT_ID)"
have="$(aws sts get-caller-identity --query Account --output text | tr -d '\r')"
if [ -z "$want" ] || [ "$have" != "$want" ]; then
  echo "wrong or unknown AWS account: credentials are for $have, AWS_ACCOUNT_ID is '${want}'" >&2
  exit 1
fi
bucket="$(envval VAULT_BUCKET)"
[ -n "$bucket" ] || { echo "VAULT_BUCKET is not set" >&2; exit 1; }

sha="$(git rev-parse --verify "$ref^{commit}")"
if ! git merge-base --is-ancestor "$sha" '@{u}' 2>/dev/null; then
  echo "warning: $sha is not on the pushed upstream branch" >&2
fi
if [ "$ref" = HEAD ] && [ -n "$(git status --porcelain -- backend contract)" ]; then
  echo "note: uncommitted changes under backend/ or contract/ are not deployed (commits only)" >&2
fi
echo "deploying: $sha ($(git log -1 --format=%s "$sha"))"

tmp="$(mixedpath "$(mktemp -d)")"
trap 'rm -rf "$tmp"' EXIT
tarball="$tmp/axelrod-$sha.tar.gz"
git archive --format=tar.gz --add-virtual-file="DEPLOYED_SHA:$sha" -o "$tarball" "$sha" backend contract
aws s3 cp "$tarball" "s3://$bucket/deploy/axelrod-$sha.tar.gz" --region "$REGION" --only-show-errors
aws s3 cp "s3://$bucket/deploy/axelrod-$sha.tar.gz" "s3://$bucket/deploy/latest.tar.gz" --region "$REGION" --only-show-errors
echo "uploaded: s3://$bucket/deploy/axelrod-$sha.tar.gz (and deploy/latest.tar.gz)"
[ "$upload_only" = 1 ] && exit 0

iid="$(envval EC2_INSTANCE_ID)"
[ -n "$iid" ] || { echo "EC2_INSTANCE_ID is not set" >&2; exit 1; }

b64="$(tr -d '\r' < backend/infra/remote_deploy.sh | base64 -w0)"
cmd="echo $b64 | base64 -d > /usr/local/bin/axelrod-deploy && chmod 755 /usr/local/bin/axelrod-deploy && /usr/local/bin/axelrod-deploy $bucket $REGION"
printf '{"commands":["%s"]}' "$cmd" > "$tmp/params.json"
cid="$(aws ssm send-command --region "$REGION" --instance-ids "$iid" \
  --document-name AWS-RunShellScript --comment "axelrod deploy ${sha:0:12}" --timeout-seconds 900 \
  --parameters "file://$tmp/params.json" --query Command.CommandId --output text | tr -d '\r')"
echo "ssm command: $cid on $iid"

status=""
for _ in $(seq 1 180); do
  status="$(aws ssm get-command-invocation --region "$REGION" --command-id "$cid" --instance-id "$iid" \
    --query Status --output text 2>/dev/null | tr -d '\r')" || status=""
  case "$status" in
    ""|Pending|InProgress|Delayed) sleep 5 ;;
    *) break ;;
  esac
done

out="$(aws ssm get-command-invocation --region "$REGION" --command-id "$cid" --instance-id "$iid" \
  --query StandardOutputContent --output text | tr -d '\r')"
printf '%s\n' "$out"
if [ "$status" != "Success" ]; then
  echo "deploy failed: ssm status ${status:-unknown}" >&2
  aws ssm get-command-invocation --region "$REGION" --command-id "$cid" --instance-id "$iid" \
    --query StandardErrorContent --output text | tr -d '\r' | tail -n 30 >&2
  exit 1
fi
echo "expected sha: $sha"
if ! printf '%s\n' "$out" | grep -qx "deployed_sha=$sha"; then
  echo "deploy failed: the instance reports a different sha" >&2
  exit 1
fi

url="$(envval API_INVOKE_URL)"
if [ -n "$url" ]; then
  echo "public healthz: $(curl -s --max-time 15 "${url%/}/healthz")"
fi
