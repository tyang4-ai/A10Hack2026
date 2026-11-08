# Presenter kit

For the laptop we present from. Nothing on stage depends on the build machine. The laptop needs no AWS credentials, because the webapp and the CLI only talk to our API.

**URL: https://ppgcikvoi0.execute-api.us-east-1.amazonaws.com/**

The webapp is at the root. The API is under `/api/v1`, health at `/healthz`, Swagger at `/docs`.

## Tabs to open

1. The webapp home: `https://ppgcikvoi0.execute-api.us-east-1.amazonaws.com/`
2. The finished run, R2 (demo-ok-2): `https://ppgcikvoi0.execute-api.us-east-1.amazonaws.com/runs/run_20261108_rzpoj7`. Click **Read** in the top bar.
3. The backup run, demo-ok-1: `https://ppgcikvoi0.execute-api.us-east-1.amazonaws.com/runs/run_20261108_pin2ve`

What to click in tab 2:

- **The chain.** In the brief, under Financial Strength, click **H-011** ("Net debt including pensions to EBITDA was 2.8 times as of September 30, 2025 ...", certainty 95). The chain shows conclusion **C-021**, then data item **D-204**, then the source page: the SHL Annual Report FY2025 (DOC-001), **page 162**, with the line highlighted. Use the Page / Text toggle to show the real PDF page.
- **ROTIC.** In Review, E-001 scores -1, and code puts its certainty at 54 because some years lack the inputs.
- **A repaired item.** In Review, D-198 or D-203 (income tax, FIXED). Click it, and the Inspector shows the diff. Check in rehearsal which one shows best.

Never click **Delete other runs** on the home page during the demo. The demo runs are protected, but the other runs are evidence.

## The CLI

On screen for about 10 seconds. Install once (PowerShell, from a clone of the repo):

```
git clone https://github.com/tyang4-ai/A10Hack2026 axelrod
cd axelrod
py -3.12 -m venv .venv
.venv\Scripts\pip install typer rich pydantic python-dotenv
```

Before the demo, in the presenter terminal:

```
$env:AXELROD_API = "https://ppgcikvoi0.execute-api.us-east-1.amazonaws.com"
$env:PUBLIC_UI_URL = "https://ppgcikvoi0.execute-api.us-east-1.amazonaws.com/runs/{run_id}"
function axelrod { .venv\Scripts\python -m backend.axelrod.cli @args }
```

The one command:

```
axelrod run "Siemens Healthineers" --ticker SHL
```

It prints the run id and the webapp link. `axelrod runs` lists the runs (read only, a safe test). Without pydantic and python-dotenv the CLI still works, but it prints no link.

Tong's terminal UI is optional: `cd frontend/cli`, `npm install`, then `node bin/axelrod-tui.js` with the same `AXELROD_API`.

## Server settings for the demo

Set on EC2 in `/etc/axelrod.env` by the build machine. The laptop never changes them.

| Setting | Value | What it does |
|---|---|---|
| `STAGE_PACING` | 3 | A 3-second pause at the start of each stage (not at approve). STOP ends it at once |
| `DEMO_FAULTS` | fetch,judge | The planted faults, labeled simulated: S-017 is a broken download (503 twice, then a declared hole), and one judge verdict per run is malformed (flagged, the judge reruns) |
| `CACHE_DOCUMENTS` | on | The gateway decides every fetch, then serves it from the S3 cache |
| `CACHE_MODELS` | record | Model calls stay live and are recorded for the replay fallback. Runs say `cache_mode: documents` |
| `BUDGET_USD` | 50 | Per run |
| `PROTECTED_RUNS` | rzpoj7, pin2ve | Delete refuses the demo run and its backup |

Check from the build machine: `bash backend/infra/remote.sh 'grep -E "^(STAGE_PACING|DEMO_FAULTS|CACHE_MODELS|BUDGET_USD)=" /etc/axelrod.env'`

## The live segment, click by click

Times are seconds from the moment you click **Approve and run**. Measured with `STAGE_PACING=3` on the deployed URL (run_20261108_0ntqmj, 3:05 AM, a headless browser with all sources approved).

| When | What happens | What you do |
|---|---|---|
| before | `axelrod run ...` prints the link. The plan streams in. Bobby's plan call lands about 10 s after launch, then the approval gate opens | Open the run. At approval: drag S-002 above S-001, reject S-016. Never touch S-006, S-009, S-010 or S-017. Click **Approve and run** |
| 0 s | Collect starts (3 s pause) | Click **Revoke** on S-006, then **Confirm revoke**, right away. Don't wait for the first fetches |
| 3 s | Fetches start. Each says `cache` and when the copy was collected | Talk through the traffic |
| about 19 s | S-006's request turns red: `R-revoke-S-006` | Point at it |
| about 20 s | S-009's off-site links go red on their own (default deny). S-010, our planted test page (simulated), comes back FLAGGED by Bedrock Guardrails | "Show only blocked and flagged" in Traffic |
| about 21 s | S-017 fails, 503 twice (planted, simulated). It becomes a declared hole | Point at it |
| about 22 s | Extract starts (3 s pause) | |
| about 27 s | The first extractor calls | Stop one goon: click a working extractor in the Agents window (or Ctrl+K, type its id, Enter), then **Stop agent**, **Confirm stop**. Its next call is blocked, the rest keep going |
| about 35 s | | **Stop run**, **Confirm stop**. The red banner counts the blocked requests |

Rehearsals (fill in with a stopwatch):

| | Launch to approval | Approval to STOP | Whole live segment | Notes |
|---|---|---|---|---|
| Rehearsal 1 | | | | |
| Rehearsal 2 | | | | |

If there isn't enough time between beats, ask the build machine to raise `STAGE_PACING` (`bash backend/infra/remote.sh --set-env STAGE_PACING=<seconds>`), and write the new value here.

## 10 minutes before

- [ ] `https://ppgcikvoi0.execute-api.us-east-1.amazonaws.com/healthz` shows `{"ok":true,"version":"0.3.1","mode":"live"}`.
- [ ] The home page lists run_20261108_rzpoj7 and run_20261108_pin2ve.
- [ ] Open the three tabs. Tab 2 on Read.
- [ ] One warm-up run: `axelrod run ...`, approve, STOP at once (about $0.15). It warms Bedrock and checks the whole path. Delete it from the home page afterwards if Delete works (it needs the EC2 role's `s3:DeleteObject`).
- [ ] The build machine confirms `STAGE_PACING=3` and `DEMO_FAULTS=fetch,judge` (command above).
- [ ] Notifications off (Windows: Do not disturb). Slack and Discord closed.
- [ ] Charger in. Phone hotspot ready.
- [ ] Browser at 100% zoom, full screen (F11), at least 1600 px wide. The desk is a grid of windows.
- [ ] Terminal: big font, `AXELROD_API`, `PUBLIC_UI_URL` and the `axelrod` function set, `axelrod runs` answers.

## If something breaks

- **Bedrock slow or throttling.** The build machine sets `CACHE_MODELS=replay`: recorded model answers, labeled `documents_and_models`. Say so. It's untested on the live segment until a rehearsal has run with `record`.
- **The webapp breaks but the API answers.** Run the webapp on the laptop, pointed at the deployed API:
  ```
  cd frontend
  npm ci
  $env:NEXT_PUBLIC_API_BASE = "https://ppgcikvoi0.execute-api.us-east-1.amazonaws.com/api/v1"
  npm run build
  npm run start
  ```
  Then open `http://localhost:3000`. From the build machine, `bash backend/infra/deploy_web.sh --ref <good commit>` redeploys a known good build.
- **The venue Wi-Fi dies.** Serve the recorded runs from the laptop. **Build this before the event**, because `npm ci` and `next build` need the internet. Use a second clone, so the online build stays as it is:
  ```
  git clone https://github.com/tyang4-ai/A10Hack2026 axelrod-offline
  cd axelrod-offline
  py -3.12 -m venv .venv
  .venv\Scripts\pip install -r backend/requirements.txt
  cd frontend
  npm ci
  npm run build
  ```
  `NEXT_PUBLIC_API_BASE` stays unset, so this build talks to `http://localhost:8000/api/v1`. To run it offline, use two terminals:
  ```
  # terminal 1, in axelrod-offline
  $env:STORE = "local"; $env:FAKE_MODELS = "1"
  .venv\Scripts\python -m uvicorn backend.axelrod.api:app --port 8000

  # terminal 2, in axelrod-offline\frontend
  npm run start
  ```
  Open `http://localhost:3000`. It lists rzpoj7 and pin2ve from `backend/data/replay/`. These are recorded replays, and the UI no longer shows a badge for that, so **say it out loud: "this is a recorded replay of a real run"**. `FAKE_MODELS=1` keeps any run you start offline away from AWS. Its answers are simulated, so say that too.
- **Last resort:** the video.

## Where things live

- The webapp is the `axelrod-web` service on EC2 (port 3000, `next start`). `bash backend/infra/deploy_web.sh` ships committed HEAD and builds it on the instance, in about 2 minutes. Logs: `journalctl -u axelrod-web`.
- The API is the `axelrod` service (port 8000), owned by the backend session (`deploy.sh`).
- The routing is API Gateway `ppgcikvoi0`: `/api/*`, `/healthz`, `/test-pages/*` and `/docs` go to port 8000, and everything else goes to the webapp. Undo with `bash backend/infra/web_routes.sh --rollback`. Details in `backend/AWS_RESOURCES.md`.
