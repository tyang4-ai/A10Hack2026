#!/usr/bin/env bash
# Run one shell command on the Axelrod EC2 instance over SSM (as root) and print its output.
# Runs on the laptop, from the repo root, with workshop credentials. No SSH key exists.
#
#   bash backend/infra/remote.sh 'tail -n 30 /var/log/axelrod-userdata.log'
#   bash backend/infra/remote.sh 'journalctl -u axelrod --no-pager -n 50'
#   bash backend/infra/remote.sh --set-env TEST_PAGE_URL=https://...   # set keys in /etc/axelrod.env, restart
#
# Settings: EC2_INSTANCE_ID and AWS_ACCOUNT_ID, from the environment or backend/.env.
# --set-env takes names, ids and URLs only. Never pass credentials: the instance uses its role.
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

[ $# -ge 1 ] || { sed -n 2,10p "$0"; exit 2; }

if [ "$1" = "--set-env" ]; then
  shift
  script=""
  for kv in "$@"; do
    k="${kv%%=*}"; v="${kv#*=}"
    [[ "$k" =~ ^[A-Z][A-Z0-9_]*$ ]] || { echo "bad key: $k" >&2; exit 2; }
    case "$k" in AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN) echo "never credentials" >&2; exit 2 ;; esac
    [[ "$v" != *"'"* && "$v" != *$'\n'* ]] || { echo "value for $k has a quote or newline" >&2; exit 2; }
    script+="sed -i '/^$k=/d' /etc/axelrod.env && printf '%s\\n' '$k=$v' >> /etc/axelrod.env && "
  done
  script+="systemctl restart axelrod && for i in \$(seq 1 30); do curl -sf localhost:8000/healthz >/dev/null && break; sleep 1; done; "
  script+="grep -E '^($(IFS='|'; for kv in "$@"; do printf '%s|' "${kv%%=*}"; done | sed 's/|$//'))=' /etc/axelrod.env; "
  script+="echo healthz=\$(curl -s localhost:8000/healthz)"
else
  script="$*"
fi

want="$(envval AWS_ACCOUNT_ID)"
have="$(aws sts get-caller-identity --query Account --output text | tr -d '\r')"
if [ -z "$want" ] || [ "$have" != "$want" ]; then
  echo "wrong or unknown AWS account: credentials are for $have, AWS_ACCOUNT_ID is '${want}'" >&2
  exit 1
fi
iid="$(envval EC2_INSTANCE_ID)"
[ -n "$iid" ] || { echo "EC2_INSTANCE_ID is not set" >&2; exit 1; }

tmp="$(mixedpath "$(mktemp -d)")"
trap 'rm -rf "$tmp"' EXIT
b64="$(printf '%s\n' "$script" | base64 -w0)"
printf '{"commands":["echo %s | base64 -d | bash"]}' "$b64" > "$tmp/params.json"
cid="$(aws ssm send-command --region "$REGION" --instance-ids "$iid" --document-name AWS-RunShellScript \
  --comment "axelrod remote" --timeout-seconds 600 --parameters "file://$tmp/params.json" \
  --query Command.CommandId --output text | tr -d '\r')"

status=""
for _ in $(seq 1 120); do
  status="$(aws ssm get-command-invocation --region "$REGION" --command-id "$cid" --instance-id "$iid" \
    --query Status --output text 2>/dev/null | tr -d '\r')" || status=""
  case "$status" in
    ""|Pending|InProgress|Delayed) sleep 3 ;;
    *) break ;;
  esac
done
aws ssm get-command-invocation --region "$REGION" --command-id "$cid" --instance-id "$iid" \
  --query StandardOutputContent --output text | tr -d '\r'
if [ "$status" != "Success" ]; then
  aws ssm get-command-invocation --region "$REGION" --command-id "$cid" --instance-id "$iid" \
    --query StandardErrorContent --output text | tr -d '\r' | tail -n 30 >&2
  echo "ssm status: ${status:-unknown}" >&2
  exit 1
fi
