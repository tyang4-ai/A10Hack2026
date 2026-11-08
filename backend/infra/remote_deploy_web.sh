#!/bin/bash
# Runs ON THE EC2 INSTANCE as root. Never run it on the laptop.
# Installs the webapp tarball from the vault bucket, builds it, and restarts the axelrod-web service.
#
#   remote_deploy_web.sh <vault-bucket> <region> <api-base>
#
# infra/deploy_web.sh sends it over SSM. It never touches the axelrod service, /opt/axelrod or
# /etc/axelrod.env (the backend session owns those). Idempotent:
# - Node 22 goes to /opt/node22 (official tarball, checked against nodejs.org's SHASUMS256), once.
# - User axelrod-web and the systemd unit axelrod-web (next start on port 3000), once.
# - Each deploy builds in a fresh /opt/axelrod-web/builds/<sha>, then /opt/axelrod-web/current
#   points at it and the service restarts. So the old build keeps serving while the new one builds.
#   The two newest builds are kept.
set -euo pipefail

BUCKET="${1:?usage: remote_deploy_web.sh <vault-bucket> <region> <api-base>}"
REGION="${2:?region}"
API_BASE="${3:?api base, e.g. https://<id>.execute-api.us-east-1.amazonaws.com/api/v1}"
KEY=deploy/web-latest.tar.gz
NODE_VERSION=v22.12.0
NODE_DIR=/opt/node22
WEB=/opt/axelrod-web
export HOME=/root NEXT_TELEMETRY_DISABLED=1

# 1. Node 22, once.
if [ "$("$NODE_DIR/bin/node" -v 2>/dev/null || true)" != "$NODE_VERSION" ]; then
  t="$(mktemp -d)"
  f="node-$NODE_VERSION-linux-x64.tar.xz"
  curl -fsSL -o "$t/$f" "https://nodejs.org/dist/$NODE_VERSION/$f"
  curl -fsSL -o "$t/SHASUMS256.txt" "https://nodejs.org/dist/$NODE_VERSION/SHASUMS256.txt"
  (cd "$t" && grep " $f\$" SHASUMS256.txt | sha256sum -c -)
  rm -rf "$NODE_DIR" && mkdir -p "$NODE_DIR"
  tar -xJf "$t/$f" -C "$NODE_DIR" --strip-components=1
  rm -rf "$t"
fi
export PATH="$NODE_DIR/bin:$PATH"
echo "node=$(node -v) npm=$(npm -v)"

# 2. User and unit, once (rewritten each time so the unit stays as committed here).
id axelrod-web >/dev/null 2>&1 || useradd --system --home-dir "$WEB" --shell /sbin/nologin axelrod-web
mkdir -p "$WEB/builds"
cat > /etc/systemd/system/axelrod-web.service <<UNIT
[Unit]
Description=Axelrod webapp (Next.js, next start on port 3000)
After=network-online.target
Wants=network-online.target

[Service]
User=axelrod-web
WorkingDirectory=$WEB/current/frontend
Environment=NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PATH=$NODE_DIR/bin:/usr/bin:/bin
ExecStart=$NODE_DIR/bin/node node_modules/next/dist/bin/next start -H 0.0.0.0 -p 3000
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable axelrod-web >/dev/null 2>&1

# 3. Download and build in a fresh folder.
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
aws s3 cp "s3://$BUCKET/$KEY" "$tmp/web.tar.gz" --region "$REGION" --only-show-errors
mkdir "$tmp/x"
tar -xzf "$tmp/web.tar.gz" -C "$tmp/x"
for need in frontend contract DEPLOYED_SHA; do
  [ -e "$tmp/x/$need" ] || { echo "tarball is missing $need" >&2; exit 1; }
done
sha="$(cat "$tmp/x/DEPLOYED_SHA")"
build="$WEB/builds/$sha-$(date +%s)"   # never the folder that is serving, even for the same sha
mv "$tmp/x" "$build"
cd "$build/frontend"
npm ci --no-audit --no-fund --loglevel=error
NEXT_PUBLIC_API_BASE="$API_BASE" NODE_OPTIONS=--max-old-space-size=2048 npm run build > "$build/build.log" 2>&1 || {
  echo "build failed, last lines:" >&2; tail -n 40 "$build/build.log" >&2; exit 1; }
chown -R axelrod-web:axelrod-web "$build"

# 4. Switch and restart.
ln -sfn "$build" "$WEB/current"
systemctl restart axelrod-web
code=""
for _ in $(seq 1 60); do
  code="$(curl -s -o /dev/null -w '%{http_code}' localhost:3000/ || true)"
  [ "$code" = 200 ] && break
  sleep 1
done

# 5. Keep the two newest builds.
ls -1dt "$WEB"/builds/* | tail -n +3 | xargs -r rm -rf

echo "web_deployed_sha=$sha"
echo "web_api_base=$API_BASE"
echo "web_local_status=$code"
if [ "$code" != 200 ]; then
  echo "axelrod-web did not answer 200 on localhost:3000 within 60s, last log lines:" >&2
  journalctl -u axelrod-web --no-pager -n 30 >&2 || true
  exit 1
fi
