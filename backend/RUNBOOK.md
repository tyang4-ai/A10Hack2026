# Backend runbook (for the orchestrator)

You are the orchestrator: the main Claude Code session on the `backend` branch. You plan, delegate, check and commit. Subagents write the code. This file is your manual. `backend/PLANS.md` holds the work packages and `backend/status.md` is the board.

Start of every session: read `CLAUDE.md`, this file and `backend/status.md`, then skim `docs/BACKEND.md` and `docs/CONTRACT.md`. Go to the stage named in status.md (S0 on a fresh start).

## 1. Rules for you

1. Delegate code. You write only gate glue (under about 20 lines), status.md, DEMO.md and requirements.txt additions. Everything else goes to a subagent with a plan id.
2. One shared folder, no worktrees. Inside a stage, every file has exactly one owner (the plan's "Owns" list). Never run two plans that own the same file at the same time.
3. Only you run git, package installs and long-running servers. Only `aws-operator` changes AWS. Builders report what they need in their REPORT block.
4. This branch changes only `backend/` and `.claude/`. The guard enforces it. Contract changes follow section 7.
5. Credentials are in Gordon's environment. Never print, read, copy or ask for them. `aws sts get-caller-identity` is the only check you need. On `ExpiredToken`, `InvalidClientTokenId` or `UnrecognizedClientException`, stop AWS work, ask Gordon to refresh (section 6), and keep going on anything that doesn't need AWS.
6. The guard (`.claude/hooks/guard.py`) can deny a call or ask Gordon. Never work around a deny. Read the reason, then do it the allowed way or ask Gordon.
7. Keep your own context small. Read REPORT blocks, not code. Point subagents at files instead of pasting them. After a compaction the SessionStart hook puts status.md back in front of you.
8. If you're blocked on Gordon and he hasn't answered in about 10 minutes, take the conservative option, log it under Decisions in status.md, and carry on.
9. Label anything simulated: fixture runs, replays, the planted injection page, demo error injection, the frozen plan.
10. New code only. Never open `calhacks2026` or Gordon's vault, and never feed a run anything from `Notes/Finance/`.

## 2. Timeline

Coding starts 8:00 PM Saturday, Pacific time. A cutoff is a hard time: when you reach it, take the fallback and move on, even if the stage isn't done.

| Stage | Window | Plans (parallel inside a stage) | Gate | Cutoff and fallback |
|---|---|---|---|---|
| S0 Preflight | 8:00 to 8:10 | you | G0 | Guard self-test fails: stop, tell Gordon |
| S1 Scaffold and AWS | 8:10 to 8:45 | P-scaffold, A-setup | G1 | 9:15: Bedrock still failing means build on with `FAKE_MODELS=1` while Gordon asks an ACM organizer |
| W1 Core | 8:45 to 10:45 | B-store-api, B-gateway, B-verify, B-ingest, B-shl-plan | G2 | 11:00: fix gate failures only. B-shl-plan shrinks to S-001 plus the test page |
| W2 Pipeline | 10:45 to 12:45 | B-extract, B-reason, B-brief, B-cli, A-deploy | G3 (with the pre-smoke) | 1:15: brief keeps copied sentences only, context check moves to W3 |
| D1 Deploy check | 12:45 to 1:30 | you, aws-operator | G4 | 2:30: serve the API from the laptop instead (`STORE=aws`), the frontend points there |
| R1 Full SHL run | 1:30 to 3:00 | you, builders for fixes | G5, tag `demo-ok-1` | 3:30: a finished run must exist. Drop to priority sources only |
| W3 Demo and P1 | 3:00 to 5:15 | B-live, B-baseline, B-p1 | G6 | 5:15: no new work |
| Freeze | 5:15 to 5:30 | you | final push | 5:30: Gordon and Tong merge into main (CLAUDE.md) |

Tell Gordon right away when these happen, because Tong is waiting on them: G2 (real API on localhost for the plan and approval panels), G4 (the API Gateway URL), G5 (the finished SHL run id).

## 3. How to delegate

Launch every plan of a stage in one message, one subagent call per plan, so they run in parallel. Agents: `scaffolder` for P-scaffold, `builder` for every B-plan, `aws-operator` for every A-plan, `verifier` at gates. Use this prompt:

```
Plan <ID> from backend/PLANS.md.
Read CLAUDE.md, then backend/PLANS.md ("Shared interfaces" and "<ID>"), then the files the plan lists under "Read first".
Own only the files under "Owns". Other subagents are editing the rest of backend/ right now.
Context since the plan was written: <decisions, resource names, model ids, interface notes from earlier REPORTs, or "none">.
Finish with the REPORT block from your role card.
```

When a REPORT comes back:
1. Update the plan's row in status.md: status, files, tests, notes.
2. Handle `needs`: install packages and add them to `backend/requirements.txt`, pass interface notes to plans that haven't started, put decisions under Needs Gordon.
3. `done`: accept. `partial` or `blocked`: one more subagent call that includes the REPORT, or move the rest to the next stage. Never more than two tries per plan, then cut scope.
4. If a REPORT drifts from PLANS.md "Shared interfaces", PLANS.md wins unless you decide otherwise. If you change an interface, append a dated note under "Interface notes" at the end of PLANS.md and pass it on.

## 4. Gates

A gate is a list of commands you run yourself. Record each result in status.md, then commit and push:

```
git add backend .claude
git commit -m "<gate>: <one line>"
git push origin backend
```

From G2 on, also run the `verifier` subagent on the stage's plan ids and fix what it finds (or log cosmetic findings under Known issues).

`PY` means `.venv/Scripts/python` on Windows and `.venv/bin/python` on Linux. Run everything from the repo root.

**G0 Preflight**
1. `git status -sb` shows `backend` and a clean tree. Then `git fetch origin && git merge origin/main`.
2. `echo AXELROD_GUARD_SELFTEST` must come back denied with "Guard is live". If the text just prints, the hooks aren't running: stop and tell Gordon. (Likely fix: `python` isn't on the PATH Claude Code sees. In `.claude/settings.json` change the leading `python` to `py` in both hook commands, then restart claude.)
3. `python .claude/hooks/test_guard.py` passes every case.
4. Venv: `uv venv --python 3.12 .venv` if `uv` exists, otherwise `python -m venv .venv` (3.11 or newer). Then `PY -m pip install fastapi "uvicorn[standard]" "pydantic>=2.7" python-dotenv boto3 httpx pytest pytest-asyncio`.
5. `aws sts get-caller-identity --query Account --output text` prints `506556958358`. Another account: stop and tell Gordon. No or expired credentials: tell Gordon and carry on, A-setup waits.
6. Fill in status.md, commit "S0: preflight", push.

**G1 Scaffold and AWS**
1. `PY -m pip install -r backend/requirements.txt` succeeds.
2. `PY -c "import backend.axelrod.api, backend.axelrod.gateway, backend.axelrod.orchestrator, backend.axelrod.cli"` succeeds.
3. `PY -m pytest -q backend/tests` collects with no errors.
4. `backend/.env` has every key from `backend/.env.example` (resource names and model ids, never credentials). `backend/AWS_RESOURCES.md` lists every resource with its delete command.
5. The A-setup REPORT shows both models answering a forced tool call, the parallel-call numbers, and the guardrail flagging the injection sample while passing a clean page. Set `MAX_CONCURRENCY_SMALL` and `MAX_CONCURRENCY_LARGE` in `.env` from those numbers.

**G2 Core (after W1)**
1. `PY -m pytest -q backend/tests -m "not live"` is green.
2. The planted-error test prints every planted error caught and every clean item passing.
3. Start `PY -m uvicorn backend.axelrod.api:app --port 8001` in the background with `STORE=local FAKE_MODELS=1`. `curl -s localhost:8001/healthz` shows `"mode":"live"`. `PY backend/scripts/contract_smoke.py --base http://localhost:8001/api/v1` passes: SHL run created, plan streams in, run waits at `awaiting_approval`, approval builds a policy with allow rules and default deny, stop works, every response validates. Stop the server.
4. `PY backend/scripts/check_plan.py` passes.
5. `PY backend/scripts/ingest_one.py S-001 --pages 1-3` fetches the SHL annual report through the gateway into the local vault and prints page text (no AWS needed).
6. Verifier on the W1 plans. Then tell Gordon the real API runs locally on port 8001 (`STORE=local`), so Tong can try the plan and approval panels against it.

**G3 Pipeline (after W2), with the pre-smoke**
1. pytest green (not live).
2. Pre-smoke, live Bedrock, `STORE=local`: `PY backend/scripts/presmoke.py --source S-001 --pages <five statement pages from data/shl/plan.json>` runs every stage and prints items, pass, fixed, fail, holes, brief sentences and cost. Every stage must produce output. Fix and rerun until it does.
3. `contract_smoke.py --run <presmoke run id>`: report and summary validate.
4. CLI against the local API: `PY -m backend.axelrod.cli run "Siemens Healthineers" --ticker SHL --api http://localhost:8001/api/v1`, then `tail`, `approve --all`, `revoke`, `stop`.
5. Verifier on the W2 plans.

**G4 Deploy**
1. `curl -s <invoke-url>/healthz` is ok and `curl -s <invoke-url>/api/v1/runs` returns a list.
2. The instance runs the commit you just pushed (A-deploy's `deploy.sh` prints it).
3. Tell Gordon: `VITE_API_URL=<invoke-url>/api/v1` for Tong.

**G5 Full SHL run**
1. `PY -m backend.axelrod.cli run "Siemens Healthineers" --ticker SHL --api <invoke-url>/api/v1`, approve per `data/shl/plan.json`, `tail` until done.
2. Check: report final, rollup present, every gap declared as a hole, brief `est_pages` at most 3, cost under budget. Write the numbers in status.md: items, pass, fixed, fail, mean certainty, coverage, cost, minutes.
3. Export the fallback: `PY backend/scripts/export_run.py <run_id>` writes `backend/data/replay/<run_id>/`, which the API serves as `mode: replay` if AWS dies. Commit it.
4. `git tag demo-ok-1 && git push origin demo-ok-1`. Tell Gordon the run id.

**G6 Demo**
1. Rehearse the live part twice from the CLI against the deployed API, in A10's order: launch, approve, traffic, a blocked link-follow, revoke S-006 and see the next request to it blocked, the test page flagged and quarantined, STOP blocking the next model call. Write the exact commands in `backend/DEMO.md`.
2. pytest green, final push.

## 5. status.md

The board. Keep these sections and keep the file under 150 lines (move old detail to Log at the bottom): Now (stage, time, what's running), Plans (table: id, agent, status, tests, notes), Gates, AWS, Decisions, Known issues, Needs Gordon, Log.

Gordon reads it from his phone. Write "Needs Gordon" items so they make sense with no other context.

## 6. AWS credentials and accounts

- Workshop account `506556958358` (shown as 5065-5695-8358), us-east-1 only. Never use `--profile`: Gordon's personal account profile lives on this machine too. The guard blocks it.
- Workshop credentials expire. Gordon gets fresh ones from the Workshop Studio dashboard ("Get AWS CLI credentials"). If he set them as environment variables, he has to restart claude (`/exit`, set the variables, `claude -c` to resume). If he pasted the credentials-file version into `~/.aws/credentials`, nothing restarts. Ask him which.
- The deployed API on EC2 uses its IAM role, which never expires. Laptop credentials only matter for building, tests marked live, and the pre-smoke.
- Deletes and stops ask Gordon (guard). Every resource is in `backend/AWS_RESOURCES.md` with its delete command.

## 7. Contract changes

The contract (`contract/models.py` and friends) lives on main and Gordon owns it. Backend code adapts to it. When a builder hits something the contract can't express:
1. Prefer a workaround inside `backend/`: an internal model, a value in a check's `detail`, a new event type (the UI ignores unknown types).
2. If a change is really needed, put it under Needs Gordon: the exact field, why, and whether it's additive or breaking.
3. If Gordon approves an additive change, do it at a gate while no subagent is running: `git switch main`, edit `contract/models.py`, `python contract/gen_types.py`, `python mock/build_fixtures.py`, add a CHANGELOG line, bump VERSION, commit, push main (the guard asks Gordon), `git switch backend`, `git merge origin/main`. Then remind Gordon to tell Tong. Breaking changes need Tong's OK first.

## 8. When things go wrong

- Bedrock throttling: lower the semaphores in `.env`. Haiku carries extraction and judging, so protect its quota first.
- A gate is red after two fix rounds: cut scope per the cutoff column and log it.
- A subagent edited a file it doesn't own: have the owner reconcile it. If nobody owns it, restore it from HEAD (`git checkout -- <file>`). Log it.
- A merge conflict from origin/main: stop and tell Gordon. The branch rules in CLAUDE.md should prevent it.
- The full run fails on EC2: read `Run.error` and the events, fix, start a fresh run. No resuming.
- Unsure whether something counts as simulated: label it.
