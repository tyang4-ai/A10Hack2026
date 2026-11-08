# AWS resources

Everything Axelrod created in the workshop account, us-east-1. Kept by the aws-operator. Names, purposes and delete commands only: ARNs, account ids and settings live in `backend/.env`, which is not committed.

API Gateway invoke URL: (not created yet)

| Resource | Type | Purpose | Created | Delete command |
|---|---|---|---|---|
| `axelrod-vault-mfafxo` | S3 bucket | The vault: raw source files, page text, reports, agent transcripts, state snapshots | 2026-11-07 A-setup | `aws s3 rb s3://axelrod-vault-mfafxo --force --region us-east-1` (empties it first) |
| `axelrod-runs` | DynamoDB table (on-demand, PK `run_id` S) | One Run row per run | 2026-11-07 A-setup | `aws dynamodb delete-table --table-name axelrod-runs --region us-east-1` |
| `axelrod-events` | DynamoDB table (on-demand, PK `run_id` S, SK `seq` N) | The event log, gapless `seq` per run | 2026-11-07 A-setup | `aws dynamodb delete-table --table-name axelrod-events --region us-east-1` |
| `axelrod-tripwire` (id `svc2ecusax6e`) | Bedrock guardrail, prompt attack filter HIGH on input | `gateway.scan` tripwire on fetched text (ApplyGuardrail, source INPUT) | 2026-11-07 A-setup | `aws bedrock delete-guardrail --guardrail-identifier svc2ecusax6e --region us-east-1` (deletes all versions) |
| `axelrod-ec2` | IAM role (trusted by ec2.amazonaws.com), inline policy `axelrod-ec2-least-privilege` plus managed `AmazonSSMManagedInstanceCore` | The API's identity on EC2: Bedrock invoke on MODEL_LARGE and MODEL_SMALL, ApplyGuardrail on the tripwire, S3 on the vault, DynamoDB on the two tables. Created and updated by `backend/infra/ec2_role.py` | 2026-11-07 A-setup | Delete the instance profile first (next row), then `aws iam delete-role-policy --role-name axelrod-ec2 --policy-name axelrod-ec2-least-privilege && aws iam detach-role-policy --role-name axelrod-ec2 --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore && aws iam delete-role --role-name axelrod-ec2` |
| `axelrod-ec2` | IAM instance profile holding the role above | Attached to the EC2 instance (A-deploy) | 2026-11-07 A-setup | `aws iam remove-role-from-instance-profile --instance-profile-name axelrod-ec2 --role-name axelrod-ec2 && aws iam delete-instance-profile --instance-profile-name axelrod-ec2` |

Every resource above is tagged `project=axelrod`. Guardrail version 1 is the one in use.

## Notes

- Models: the workshop allowlist policy (`ws-bedrock-model-allowlist-policy-1` on `WSParticipantRole`) explicitly denies Sonnet 5.5 and Haiku 5.5. In use: `us.anthropic.claude-sonnet-5` (large) and `us.anthropic.claude-haiku-4-5-20251001-v1:0` (small). If organizers allow 5.5, change `MODEL_*` in `.env` and re-run `backend/infra/ec2_role.py` so the role's policy follows.
- Checks in `backend/infra/` (run from the repo root with `backend/.env` loaded): `converse_smoke.py` (one forced-tool call per model), `concurrency_test.py` (50 small, 8 large in parallel), `guardrail_test.py` (injection flagged, clean passes), `ec2_role.py` (create or update the role, `--dry-run` prints the policy).
