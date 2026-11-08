#!/usr/bin/env bash
# Ship the webapp (frontend/) to EC2 next to the API. Runs on the laptop, from the repo root,
# with workshop credentials. Never touches the axelrod service, deploy.sh or /etc/axelrod.env.
#
#   bash backend/infra/deploy_web.sh                # ship HEAD, build on the instance, restart axelrod-web
#   bash backend/infra/deploy_web.sh --upload-only  # ship HEAD to S3 only
#   bash backend/infra/deploy_web.sh --ref <commit> # ship another commit (a rollback, for example)
#
# 1. git archive of the commit (frontend/ and contract/, since the webapp imports contract/types.ts)
#    plus DEPLOYED_SHA, uploaded to s3://$VAULT_BUCKET/deploy/web-<sha>.tar.gz and copied to
#    deploy/web-latest.tar.gz.
# 2. aws ssm send-command runs infra/remote_deploy_web.sh on $EC2_INSTANCE_ID: Node 22 (once),
#    npm ci and next build with NEXT_PUBLIC_API_BASE=<invoke url>/api/v1 in a fresh folder,
#    then the axelrod-web service (next start, port 3000) restarts on it.
#
# Settings: the real environment wins, then backend/.env (VAULT_BUCKET, EC2_INSTANCE_ID,
# AWS_ACCOUNT_ID, API_INVOKE_URL). No credentials are read or written here.
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
mixedpath() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi; }

[ -d backend/infra ] && [ -d frontend ] && [ -d contract ] || { echo "run from the repo root" >&2; exit 2; }
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
iid="$(envval EC2_INSTANCE_ID)"
url="$(envval API_INVOKE_URL)"
[ -n "$bucket" ] || { echo "VAULT_BUCKET is not set" >&2; exit 1; }
[ -n "$url" ] || { echo "API_INVOKE_URL is not set" >&2; exit 1; }
api_base="${url%/}/api/v1"

sha="$(git rev-parse --verify "$ref^{commit}")"
if [ "$ref" = HEAD ] && [ -n "$(git status --porcelain -- frontend contract)" ]; then
  echo "note: uncommitted changes under frontend/ or contract/ are not deployed (commits only)" >&2
fi
echo "deploying webapp: $sha ($(git log -1 --format=%s "$sha"))"
echo "api base baked into the build: $api_base"

tmp="$(mixedpath "$(mktemp -d)")"
trap 'rm -rf "$tmp"' EXIT
tarball="$tmp/web-$sha.tar.gz"
git archive --format=tar.gz --add-virtual-file="DEPLOYED_SHA:$sha" -o "$tarball" "$sha" frontend contract
aws s3 cp "$tarball" "s3://$bucket/deploy/web-$sha.tar.gz" --region "$REGION" --only-show-errors
aws s3 cp "s3://$bucket/deploy/web-$sha.tar.gz" "s3://$bucket/deploy/web-latest.tar.gz" --region "$REGION" --only-show-errors
echo "uploaded: s3://$bucket/deploy/web-$sha.tar.gz (and deploy/web-latest.tar.gz)"
[ "$upload_only" = 1 ] && exit 0
[ -n "$iid" ] || { echo "EC2_INSTANCE_ID is not set" >&2; exit 1; }

b64="$(tr -d '\r' < backend/infra/remote_deploy_web.sh | base64 -w0)"
cmd="echo $b64 | base64 -d > /usr/local/bin/axelrod-deploy-web && chmod 755 /usr/local/bin/axelrod-deploy-web && /usr/local/bin/axelrod-deploy-web $bucket $REGION $api_base"
printf '{"commands":["%s"]}' "$cmd" > "$tmp/params.json"
cid="$(aws ssm send-command --region "$REGION" --instance-ids "$iid" \
  --document-name AWS-RunShellScript --comment "axelrod-web deploy ${sha:0:12}" --timeout-seconds 1200 \
  --parameters "file://$tmp/params.json" --query Command.CommandId --output text | tr -d '\r')"
echo "ssm command: $cid on $iid (npm ci and next build take a few minutes)"

status=""
for _ in $(seq 1 240); do
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
  echo "webapp deploy failed: ssm status ${status:-unknown}" >&2
  aws ssm get-command-invocation --region "$REGION" --command-id "$cid" --instance-id "$iid" \
    --query StandardErrorContent --output text | tr -d '\r' | tail -n 40 >&2
  exit 1
fi
if ! printf '%s\n' "$out" | grep -qx "web_deployed_sha=$sha"; then
  echo "webapp deploy failed: the instance reports a different sha" >&2
  exit 1
fi
echo "public webapp: $(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "${url%/}/") at ${url%/}/ (needs port 3000 open and the \$default route)"
echo "public api:    $(curl -s --max-time 15 "${url%/}/healthz")"
