#!/bin/bash
# EC2 user data for the Axelrod API (Amazon Linux 2023). A template: infra/render_userdata.py
# fills every placeholder (a NAME between two @ pairs) from the environment, backend/.env as the
# fallback, and refuses if one is left. Never put credentials here: on EC2, boto3 uses the
# instance role axelrod-ec2.
#
# First boot: python3.11, a system user, /opt/axelrod with a venv, /etc/axelrod.env, the
# systemd unit `axelrod`, then remote_deploy.sh pulls deploy/latest.tar.gz from the vault bucket
# (infra/deploy.sh --upload-only puts it there before launch) and starts the service.
# Log: /var/log/axelrod-userdata.log
set -euxo pipefail
exec > >(tee -a /var/log/axelrod-userdata.log) 2>&1

dnf install -y python3.11 python3.11-pip tar gzip

id axelrod >/dev/null 2>&1 || useradd --system --home-dir /opt/axelrod --shell /sbin/nologin axelrod
mkdir -p /opt/axelrod
[ -x /opt/axelrod/.venv/bin/python ] || python3.11 -m venv /opt/axelrod/.venv
/opt/axelrod/.venv/bin/pip install --quiet --disable-pip-version-check --upgrade pip

# Runtime settings. Names, ids, model ids and prices only.
cat > /etc/axelrod.env <<'EOF'
STORE=aws
AWS_REGION=@@AWS_REGION@@
AWS_DEFAULT_REGION=@@AWS_REGION@@
MODEL_LARGE=@@MODEL_LARGE@@
MODEL_SMALL=@@MODEL_SMALL@@
GUARDRAIL_ID=@@GUARDRAIL_ID@@
GUARDRAIL_VERSION=@@GUARDRAIL_VERSION@@
VAULT_BUCKET=@@VAULT_BUCKET@@
RUNS_TABLE=@@RUNS_TABLE@@
EVENTS_TABLE=@@EVENTS_TABLE@@
MAX_CONCURRENCY_SMALL=@@MAX_CONCURRENCY_SMALL@@
MAX_CONCURRENCY_LARGE=@@MAX_CONCURRENCY_LARGE@@
BUDGET_USD=@@BUDGET_USD@@
PRICES_JSON='@@PRICES_JSON@@'
FAKE_MODELS=0
DATA_DIR=/var/lib/axelrod
AXELROD_ENV_FILE=
EOF
chown root:axelrod /etc/axelrod.env
chmod 640 /etc/axelrod.env

cat > /etc/systemd/system/axelrod.service <<'EOF'
[Unit]
Description=Axelrod API (FastAPI on uvicorn)
After=network-online.target
Wants=network-online.target

[Service]
User=axelrod
Group=axelrod
WorkingDirectory=/opt/axelrod
EnvironmentFile=/etc/axelrod.env
Environment=PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
StateDirectory=axelrod
ExecStart=/opt/axelrod/.venv/bin/uvicorn backend.axelrod.api:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable axelrod

# The install steps, shared with infra/deploy.sh (base64 of infra/remote_deploy.sh).
echo '@@REMOTE_DEPLOY_B64@@' | base64 -d > /usr/local/bin/axelrod-deploy
chmod 755 /usr/local/bin/axelrod-deploy
/usr/local/bin/axelrod-deploy '@@VAULT_BUCKET@@' '@@AWS_REGION@@'
