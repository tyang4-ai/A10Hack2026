"""Fill infra/userdata.sh with settings and write the EC2 user data file.

Values come from the environment, with backend/.env as the fallback (the real environment wins).
Only names, ids, model ids, prices and limits go in. Credentials never do: the instance uses
its role. Fails if a placeholder is left or a value could break the env file.

Run from the repo root:
    .venv/Scripts/python backend/infra/render_userdata.py <out-file>

Then launch with `aws ec2 run-instances ... --user-data file://<out-file>` (see AWS_RESOURCES.md).
"""
import base64
import os
import re
import sys
from pathlib import Path

INFRA = Path(__file__).resolve().parent
ENV_FILE = INFRA.parent / ".env"
KEYS = [
    "AWS_REGION", "MODEL_LARGE", "MODEL_SMALL", "GUARDRAIL_ID", "GUARDRAIL_VERSION",
    "VAULT_BUCKET", "RUNS_TABLE", "EVENTS_TABLE", "MAX_CONCURRENCY_SMALL",
    "MAX_CONCURRENCY_LARGE", "BUDGET_USD", "PRICES_JSON",
]
FORBIDDEN = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")


def settings() -> dict[str, str]:
    from dotenv import dotenv_values

    raw: dict[str, str] = {}
    if ENV_FILE.is_file():
        raw.update({k: v for k, v in dotenv_values(ENV_FILE, encoding="utf-8").items() if v})
    raw.update({k: v for k, v in os.environ.items() if v})
    missing = [k for k in KEYS if not raw.get(k, "").strip()]
    if missing:
        raise SystemExit(f"missing settings: {', '.join(missing)}")
    out = {k: raw[k].strip() for k in KEYS}
    if out["AWS_REGION"] != "us-east-1":
        raise SystemExit("AWS_REGION must be us-east-1")
    for k, v in out.items():
        if "'" in v or "\n" in v or "@@" in v:
            raise SystemExit(f"{k} has a quote, newline or @@ in it")
    return out


def render() -> str:
    text = (INFRA / "userdata.sh").read_text(encoding="utf-8").replace("\r\n", "\n")
    deploy = (INFRA / "remote_deploy.sh").read_text(encoding="utf-8").replace("\r\n", "\n")
    values = settings()
    values["REMOTE_DEPLOY_B64"] = base64.b64encode(deploy.encode("utf-8")).decode("ascii")
    for k, v in values.items():
        text = text.replace(f"@@{k}@@", v)
    left = re.findall(r"@@[A-Z0-9_]+@@", text)
    if left:
        raise SystemExit(f"unfilled placeholders: {sorted(set(left))}")
    if any(k in text for k in FORBIDDEN):
        raise SystemExit("credential variable name found in user data, refusing")
    if len(text.encode("utf-8")) > 16384:
        raise SystemExit("user data is over the 16 KB EC2 limit")
    return text


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    out = Path(sys.argv[1])
    out.write_text(render(), encoding="utf-8", newline="\n")
    print(f"user data written: {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
