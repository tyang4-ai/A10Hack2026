# Integration and AWS deploy (the second Claude Code session)

For the integrator: a fresh Claude Code session that takes over when the backend orchestrator is done. Goal: one URL on AWS that serves Tong's webapp wired to our backend, rehearsed for the 3-minute demo, plus a kit for the presenter laptop. Gordon presents from his laptop, so nothing on stage may depend on the build machine.

## When to start

All three must be true:
1. `backend/status.md` on `origin/backend` starts with READY FOR INTEGRATION (step 11 of `docs/BACKEND_V2.md`).
2. Tong has pushed his latest webapp and his CLI version.
3. Gordon has ended the orchestrator's session, so two sessions never deploy to the same instance.

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
aws sts get-caller-identity --query Account --output text     # 506556958358
claude --permission-mode acceptEdits
```

The merges shouldn't conflict, since each branch only touched its own folder. If one does, stop and tell Gordon. `backend/.env` holds resource names and model ids (and the API key, if Gordon set one). It is gitignored.

## Read first

`CLAUDE.md`, this file, `docs/CONTRACT.md` section 13, the READY FOR INTEGRATION block in `backend/status.md`, `backend/DEMO.md`, the demo script in `docs/PLAN.md`, `backend/AWS_RESOURCES.md`, `frontend/CONTRACT_REQUESTS.md`. Rules from `backend/RUNBOOK.md` section 1 still hold: never print or ask for credentials, us-east-1 only, deletes go through Gordon, label anything simulated or cached, new code only.

## Work

1. **Local wiring check.** Backend from this folder with `STORE=local` on port 8001. It serves the recorded runs in `backend/data/replay/`. Frontend: `npm ci`, then `frontend/.env.local` with `NEXT_PUBLIC_API_BASE=http://localhost:8001/api/v1`, then `npm run build` and `npm run start -- -p 3000`. Check that the home page lists the runs, the finished run shows the brief, the agent report, charts and source pages, and no API call fails. On a `FAKE_MODELS=1` run, check that stop agent, chat, resume and rerun work from the UI. Use a headless browser if one is installed. Otherwise ask Gordon to click through for two minutes.

2. **Frontend fixes for real data**, unless Tong has pushed them already:
   - Charts must handle negative values, several series and compact labels (€15.7bn). Today the R&D chart is blank and the debt chart stacks three series on one bar.
   - The full gap register goes outside `#brief`, so the brief prints in 3 pages.
   - A badge for `cache_mode` other than `off`.
   - `src/lib/agentApi.ts` gives way to the contract types.

   This is Tong's folder. Send him the list first. Change his code only if he's offline and Gordon says yes, keep the change small, and tell Tong what you changed.

3. **Deploy the webapp on AWS**, on the existing EC2 instance behind the existing API Gateway (use the aws-operator):
   - Node 22 on the instance, and the webapp built there with `NEXT_PUBLIC_API_BASE=<invoke-url>/api/v1`. The instance has internet, so Google Fonts load at build time.
   - A systemd unit `axelrod-web` running `next start` on port 3000. Opening port 3000 goes through the guard, so Gordon confirms it.
   - API Gateway routes: `ANY /api/{proxy+}`, `GET /healthz` and `ANY /test-pages/{proxy+}` go to port 8000. `$default` goes to port 3000 (pages and `/_next/` assets).
   - One command ships both services: extend `backend/infra/deploy.sh` or add `backend/infra/deploy_web.sh`.
   - Check: the invoke URL opens the webapp, and the API answers under `/api/v1`.

4. **API key: ask Gordon.** During judging the URL is public, and it will be in a public repo. If he says yes, set `AXELROD_API_KEY` on the instance. Have the webapp send `X-Axelrod-Key` from `NEXT_PUBLIC_AXELROD_KEY` (one line in Tong's `api.ts`, so tell Tong), and the CLI from an environment variable. Log it under "Credentials" in `backend/AWS_RESOURCES.md`: the name and where it lives, never the value.

5. **Rehearse the 3-minute demo twice** on the deployed URL, with a stopwatch, following `docs/PLAN.md` and `backend/DEMO.md`. Do the CLI launch from the laptop's point of view: Tong's CLI if it works against the invoke URL, otherwise `python -m backend.axelrod.cli`. It is on screen for about 10 seconds. Fix what breaks, and write the real timings into `backend/DEMO.md`.

6. **Write `docs/LAPTOP.md`**, the presenter kit:
   - The tabs to open: the webapp, the finished run, the source page to click into.
   - The CLI: the minimal install and the one command. The laptop needs no AWS credentials, because the CLI only talks to our API.
   - A 10-minute pre-demo checklist: healthz, one warm-up run, `DEMO_PACING` set, notifications off, charger.
   - The offline fallback if the venue Wi-Fi dies: the backend locally with `STORE=local` serving the recorded runs (labeled), plus the frontend locally pointed at it. Last resort: the video.

7. **Hand over.** Push the `integration` branch (the guard asks Gordon). At 5:30 Gordon and Tong merge it into `main`, tag `demo-ok-final`, and make `main` the default branch on GitHub.

## Cutoffs

- **4:45 AM:** deployed and rehearsed once.
- **5:15 AM:** second rehearsal and `docs/LAPTOP.md` done.
- **5:30 AM:** freeze. If the deploy isn't working by 4:45, present from the laptop with the local fallback and say so.
