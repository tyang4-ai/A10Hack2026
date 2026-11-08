#!/bin/bash
# Runs ON THE EC2 INSTANCE as root. Never run it on the laptop.
# Installs the code tarball from the vault bucket and restarts the axelrod service.
#
#   remote_deploy.sh <vault-bucket> [region]
#
# userdata.sh runs it on first boot, and infra/deploy.sh sends it over SSM on every redeploy,
# so this file is the one place the install steps live. It uses the instance role for S3,
# keeps /opt/axelrod/.venv, and replaces backend/, contract/ and DEPLOYED_SHA.
set -euo pipefail

BUCKET="${1:?usage: remote_deploy.sh <vault-bucket> [region]}"
REGION="${2:-us-east-1}"
APP=/opt/axelrod
KEY=deploy/latest.tar.gz

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

aws s3 cp "s3://$BUCKET/$KEY" "$tmp/app.tar.gz" --region "$REGION" --only-show-errors
mkdir "$tmp/x"
tar -xzf "$tmp/app.tar.gz" -C "$tmp/x"
for need in backend contract DEPLOYED_SHA; do
  [ -e "$tmp/x/$need" ] || { echo "tarball is missing $need" >&2; exit 1; }
done

mkdir -p "$APP"
rm -rf "$APP/backend" "$APP/contract" "$APP/DEPLOYED_SHA"
mv "$tmp/x/backend" "$tmp/x/contract" "$tmp/x/DEPLOYED_SHA" "$APP/"
chown -R root:root "$APP/backend" "$APP/contract" "$APP/DEPLOYED_SHA"

"$APP/.venv/bin/pip" install --quiet --disable-pip-version-check -r "$APP/backend/requirements.txt"

systemctl daemon-reload
systemctl restart axelrod

health=""
for _ in $(seq 1 60); do
  if health="$(curl -sf localhost:8000/healthz)"; then
    break
  fi
  sleep 1
done

echo "deployed_sha=$(cat "$APP/DEPLOYED_SHA")"
echo "healthz=$(curl -s localhost:8000/healthz)"
if [ -z "$health" ]; then
  echo "service did not answer /healthz within 60s, last log lines:" >&2
  journalctl -u axelrod --no-pager -n 30 >&2 || true
  exit 1
fi
