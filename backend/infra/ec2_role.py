"""Create or update the IAM role and instance profile `axelrod-ec2` for the API's EC2 instance.

Least privilege: Bedrock invoke/converse on MODEL_LARGE and MODEL_SMALL (their inference
profiles and the foundation models those profiles route to), ApplyGuardrail on GUARDRAIL_ID,
S3 on VAULT_BUCKET, DynamoDB on RUNS_TABLE and EVENTS_TABLE. Plus AmazonSSMManagedInstanceCore
so the instance is reachable through SSM without an SSH key.

Re-running it is safe: an existing role or profile is kept and the inline policy is overwritten,
so a model change only needs this script run again with the new MODEL_* values.

The account id comes from STS at run time, so nothing account-specific is committed.

Run from the repo root (load backend/.env into the environment first):
    set -a; . backend/.env; set +a
    .venv/Scripts/python backend/infra/ec2_role.py            # create or update
    .venv/Scripts/python backend/infra/ec2_role.py --dry-run  # print the policy only
"""
import json
import os
import sys

import boto3
from botocore.exceptions import ClientError

NAME = "axelrod-ec2"
POLICY_NAME = "axelrod-ec2-least-privilege"
SSM_POLICY = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
TAGS = [{"Key": "project", "Value": "axelrod"}]

TRUST = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "ec2.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}


def model_arns(bedrock, region: str, account: str, model_id: str) -> list[str]:
    """Inference profile ARN plus every foundation-model ARN it routes to."""
    try:
        prof = bedrock.get_inference_profile(inferenceProfileIdentifier=model_id)
    except ClientError:
        # A plain foundation model id, not a profile.
        return [f"arn:aws:bedrock:{region}::foundation-model/{model_id}"]
    return [prof["inferenceProfileArn"]] + [m["modelArn"] for m in prof["models"]]


def build_policy(region: str, account: str) -> dict:
    bedrock = boto3.client("bedrock", region_name=region)
    models: list[str] = []
    for key in ("MODEL_LARGE", "MODEL_SMALL"):
        models += model_arns(bedrock, region, account, os.environ[key])
    bucket = os.environ["VAULT_BUCKET"]
    tables = [
        f"arn:aws:dynamodb:{region}:{account}:table/{os.environ[k]}"
        for k in ("RUNS_TABLE", "EVENTS_TABLE")
    ]
    guardrail = f"arn:aws:bedrock:{region}:{account}:guardrail/{os.environ['GUARDRAIL_ID']}"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "BedrockModels",
                "Effect": "Allow",
                "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                "Resource": sorted(set(models)),
            },
            {
                "Sid": "BedrockGuardrail",
                "Effect": "Allow",
                "Action": ["bedrock:ApplyGuardrail"],
                "Resource": [guardrail],
            },
            {
                "Sid": "VaultList",
                "Effect": "Allow",
                "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
                "Resource": [f"arn:aws:s3:::{bucket}"],
            },
            {
                "Sid": "VaultObjects",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject"],
                "Resource": [f"arn:aws:s3:::{bucket}/*"],
            },
            {
                # DELETE /runs/{id} (contract 0.3.1): a run's own vault folder only, never cache/, archive/ or deploy/
                "Sid": "VaultRunDeletes",
                "Effect": "Allow",
                "Action": ["s3:DeleteObject"],
                "Resource": [f"arn:aws:s3:::{bucket}/runs/*"],
            },
            {
                "Sid": "Tables",
                "Effect": "Allow",
                "Action": [
                    "dynamodb:DescribeTable",
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:Query",
                    "dynamodb:Scan",
                    "dynamodb:BatchGetItem",
                    "dynamodb:BatchWriteItem",
                    "dynamodb:DeleteItem",
                ],
                "Resource": tables,
            },
        ],
    }


def main() -> int:
    region = os.environ.get("AWS_REGION", "us-east-1")
    account = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
    policy = build_policy(region, account)
    if "--dry-run" in sys.argv:
        print(json.dumps(policy, indent=2))
        return 0

    iam = boto3.client("iam", region_name=region)
    try:
        iam.create_role(
            RoleName=NAME,
            AssumeRolePolicyDocument=json.dumps(TRUST),
            Description="Axelrod API on EC2: Bedrock, guardrail, vault bucket, run tables",
            Tags=TAGS,
        )
        print(f"role {NAME}: created")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
        print(f"role {NAME}: exists, kept")

    iam.put_role_policy(RoleName=NAME, PolicyName=POLICY_NAME, PolicyDocument=json.dumps(policy))
    print(f"inline policy {POLICY_NAME}: written ({len(policy['Statement'])} statements)")

    iam.attach_role_policy(RoleName=NAME, PolicyArn=SSM_POLICY)
    print("managed policy AmazonSSMManagedInstanceCore: attached")

    try:
        iam.create_instance_profile(InstanceProfileName=NAME, Tags=TAGS)
        print(f"instance profile {NAME}: created")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
        print(f"instance profile {NAME}: exists, kept")

    roles = iam.get_instance_profile(InstanceProfileName=NAME)["InstanceProfile"]["Roles"]
    if not any(r["RoleName"] == NAME for r in roles):
        iam.add_role_to_instance_profile(InstanceProfileName=NAME, RoleName=NAME)
        print(f"role {NAME} added to instance profile")
    else:
        print(f"role {NAME} already in instance profile")
    return 0


if __name__ == "__main__":
    sys.exit(main())
