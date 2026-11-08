# Integration and AWS deploy (the second Claude Code session)

For the integrator: a second Claude Code session that runs next to the backend session from Sun 1 AM. Goal: one URL on AWS that serves Tong's webapp wired to our backend, rehearsed in person for the 3-minute demo, plus a kit for the presenter laptop. Gordon presents from his laptop, so nothing on stage may depend on the build machine.

## When to start

Now (Gordon, Sun 1 AM). Every call the webapp makes already has a route on the deployed backend (contract 0.3.0, `contract_smoke` 76/76 on EC2). Resume answers 409 "not built yet" until V6 lands. CORS is open. The backend session keeps working on `backend` (fixes, then R2), so you share EC2 with it until it ends:

| | Backend session | You |
|---|---|---|
| `backend/` code, the `axelrod` service (port 8000) and its env, `deploy.sh`, `remote.sh --set-env` | owns | never touch |
| The webapp wiring, a new `axelrod-web` service (port 3000), the new `$default` API Gateway route, a new `backend/infra/deploy_web.sh`, `docs/LAPTOP.md` | never touches | own |
| Demo runs on EC2 (`run_20261108_pin2ve`, and R2 once picked) | creates R2 | read only: never stop, revoke, rerun, chat or comment on them |

No new runs on EC2 while `backend/status.md` says R2 is running. Test the controls that change a run on a local backend (step 1). When `backend/status.md` starts with `DEMO RUN: <run id>`, run `git merge --no-edit origin/backend`. The backend session has deployed it already.

## Setup (Gordon runs this before starting claude)

Agents and hooks load when a session starts, so the folder must already be on the integration branch with `backend` merged in. PowerShell, next to the orchestrator's folder:

```
cd "B:\Coding\Hackathon Stuff\A10 2026"
git clone https://github.com/tyang4-ai/A10Hack2026 axelrod-integrate
cd axelrod-integrate
git switch -c integration origin/main
git merge --no-edit origin/backend
git merge --no-edit origin/frontend
copy <orchestrator folder>\backend\.env backend\.env
aws sts get-caller-identity --query Account --output text     # must print the workshop account id
claude --permission-mode acceptEdits
```

The merges shouldn't conflict, since each branch only touched its own folder. If one does, stop and tell Gordon. `backend/.env` holds resource names and model ids (and the API key, if Gordon set one). It is gitignored.

## Read first

`CLAUDE.md`, this file, `docs/CONTRACT.md` sections 13 and 14, the top of `backend/status.md`, `backend/DEMO.md`, the demo script in `docs/PLAN.md`, `backend/AWS_RESOURCES.md`, `frontend/CONTRACT_REQUESTS.md`. Rules from `backend/RUNBOOK.md` section 1 still hold: never print or ask for credentials, us-east-1 only, deletes go through Gordon, label anything simulated or cached, new code only.

## Work

1. **Local wiring check.** Backend from this folder with `STORE=local` on port 8001. It serves the recorded runs in `backend/data/replay/`. Frontend: `npm ci`, then `frontend/.env.local` with `NEXT_PUBLIC_API_BASE=http://localhost:8001/api/v1`, then `npm run build` and `npm run start -- -p 3000`. Check that the home page lists the runs, the finished run shows the brief, the agent report, charts and source pages, and no API call fails. On a `FAKE_MODELS=1` run, check that stop agent, chat, rerun one agent, rerun from a step, comments and (once V6 lands) resume work from the UI. Then point the webapp at the deployed API (`NEXT_PUBLIC_API_BASE=<invoke-url>/api/v1`) and click through `run_20261108_pin2ve`, read only. Use a headless browser if one is installed. Otherwise ask Gordon to click through for two minutes.

2. **Frontend fixes: Tong shipped them** in `ca76901` and `32c02f9`: Bobby first and his goons grouped, his calls in the log, charts on real data (negatives, several series, compact labels), the gap register outside `#brief`, the CACHED badge, contract types instead of `agentApi.ts`, Resume on the stop banner, and `X-Axelrod-Key`. Check each one against real data, don't redo them. Anything still broken goes to Tong first. This is his folder: change his code only if he's offline and Gordon says yes, keep the change small, and tell Tong what you changed.

3. **Deploy the webapp on AWS**, on the existing EC2 instance behind the existing API Gateway (use the aws-operator):
   - Node 22 on the instance, and the webapp built there with `NEXT_PUBLIC_API_BASE=<invoke-url>/api/v1`. The instance has internet, so Google Fonts load at build time.
   - A systemd unit `axelrod-web` running `next start` on port 3000. Opening port 3000 goes through the guard, so Gordon confirms it.
   - API Gateway routes: `ANY /api/{proxy+}`, `GET /healthz` and `ANY /test-pages/{proxy+}` keep going to port 8000. Check they exist and leave them alone. Add `$default` to port 3000 (pages and `/_next/` assets).
   - One command ships the webapp: a new `backend/infra/deploy_web.sh`. Leave `deploy.sh` and the `axelrod` service to the backend session.
   - Check: the invoke URL opens the webapp, and the API answers under `/api/v1`.

4. **API key: ask Gordon.** During judging the URL is public, and it will be in a public repo. If he says yes, set `AXELROD_API_KEY` on the instance. Have the webapp send `X-Axelrod-Key` from `NEXT_PUBLIC_AXELROD_KEY` (one line in Tong's `api.ts`, so tell Tong), and the CLI from an environment variable. Log it under "Credentials" in `backend/AWS_RESOURCES.md`: the name and where it lives, never the value.

5. **Rehearse in person, no script** (Gordon's call: we demo in person). After the DEMO RUN line is up, Gordon and Tong click through the 3-minute demo in `docs/PLAN.md` twice on the deployed URL, with a stopwatch. You watch the traffic and the logs and fix what breaks in your scope. Do the CLI launch from the laptop's point of view: Tong's CLI if it works against the invoke URL, otherwise `python -m backend.axelrod.cli`. It is on screen for about 10 seconds. `DEMO_PACING` slows the fetches so there's time to revoke S-006 and stop a goon: ask the backend session to set the value that worked (or Gordon, once it has ended). Write the timings and that value into `docs/LAPTOP.md`.

6. **Write `docs/LAPTOP.md`**, the presenter kit:
   - The tabs to open: the webapp, the finished run, the source page to click into.
   - The CLI: the minimal install and the one command. The laptop needs no AWS credentials, because the CLI only talks to our API.
   - A 10-minute pre-demo checklist: healthz, one warm-up run, `DEMO_PACING` set, notifications off, charger.
   - The offline fallback if the venue Wi-Fi dies: the backend locally with `STORE=local` serving the recorded runs (labeled), plus the frontend locally pointed at it. Last resort: the video.

7. **Hand over.** Push the `integration` branch (the guard asks Gordon). At 5:30 Gordon and Tong merge it into `main`, tag `demo-ok-final`, and make `main` the default branch on GitHub.

## Cutoffs

- **2:30 AM:** the webapp deployed on the invoke URL.
- **3:45 AM:** first in-person rehearsal (R2 is due by 3:15).
- **5:15 AM:** second rehearsal and `docs/LAPTOP.md` done.
- **5:30 AM:** freeze. If the deploy isn't working by 3:30, present from the laptop: the frontend running locally, pointed at the deployed API. Say so.
