# Presenter kit

For the laptop we present from. Nothing on stage depends on the build machine. The laptop needs no AWS credentials, because the webapp and the CLI only talk to our API.

**URL: https://ppgcikvoi0.execute-api.us-east-1.amazonaws.com/**

The webapp is at the root. The API is under `/api/v1`, health at `/healthz`, Swagger at `/docs`.

**Say on stage:** the documents come from our cache (collected before the event), and the agents' model answers are replayed from a recorded live run. Only agent chat is live. The plan is frozen, and S-010 (the test page) and S-017 (the broken download) are planted. All of these are labeled in the UI or here, and must be said out loud.

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

Final since 5:10 AM (`backend/status.md`, `backend/DEMO.md`). Set on EC2 in `/etc/axelrod.env` by the build machine. The laptop never changes them.

| Setting | Value | What it does |
|---|---|---|
| `CACHE_MODELS` | replay_only | Every agent model call is answered from two recorded live runs, at $0: approve-all (run_20261108_5rzsat) and the stage click path (run_20261108_etowmo). Only agent chat calls Bedrock live. A call with no recording is blocked and becomes a declared hole, never a live call. Runs say `cache_mode: documents_and_models` |
| `CACHE_DOCUMENTS` | on | The gateway decides every fetch, then serves it from the S3 cache |
| `STAGE_PACING` | 1 | Seconds of pause at the start of each stage (not at approve). STOP ends it at once |
| `FETCH_PACING` | 1.2 | Seconds before each fetch decision. A revoke in that time still blocks it |
| `VERIFY_PACING` | 3 | Seconds per judge call, so verification visibly takes time |
| `DEMO_FAULTS` | fetch,judge | The planted faults, labeled simulated: S-017 is a broken download (503 twice, then a declared hole), and one judge verdict per run is malformed (flagged, the judge reruns) |
| `MAX_CONCURRENCY_SMALL`, `MAX_CONCURRENCY_LARGE` | 48, 8 | Goons in parallel |
| `BUDGET_USD` | 50 | Per run |
| `PROTECTED_RUNS` | rzpoj7, pin2ve | Delete refuses the demo run and its backup |

Check from the build machine: `bash backend/infra/remote.sh 'grep -E "^(CACHE_MODELS|STAGE_PACING|FETCH_PACING|VERIFY_PACING|DEMO_FAULTS)=" /etc/axelrod.env'`

## The live segment, click by click

`backend/DEMO.md` has the backend session's click script. The short version:

**How long a run takes now** (timed replays with scripted clicks, 5:10 AM, both $0):

| Path | Launch to done | Approve to done |
|---|---|---|
| Stage click path: S-002 above S-001, S-016 rejected, S-006 revoked at collect start (run_20261108_f5eznb) | 103 s | 97 s (revoke at +7 s) |
| Every source approved, nothing else (run_20261108_zef836) | 129 s | 123 s |

With a person clicking, expect about 1:55 to 2:15 from launch to a finished run. In both timed replays, one judge call had no recording: it shows as blocked and becomes a hole. Expect that one red `model.invoke` line, and don't present it as a block.

**Click only one of the two recorded paths.** Any other clicks (another drag, another reject, revoking later than collect start) make later model calls miss: they show as blocked and become holes.

The beats, in order (times from the stage click path: launch at 0 s, approve at about 6 s):

| Order | What happens | What you do |
|---|---|---|
| 1 | `axelrod run ...` prints the link. The plan streams in (frozen, say so). Bobby's plan call shows, then the approval gate opens | Open the run. Approve the priority sources, drag S-002 above S-001, reject S-016. Never touch S-006, S-009, S-010 or S-017. Click **Approve and run** |
| 2 | Collect starts after a 1 s pause | **Right away, click Revoke on S-006, then Confirm revoke** (the timed replay revoked it at +7 s) |
| 3 | Fetches, about 1.2 s apart. Each says `cache` and when the copy was collected | Talk through the traffic |
| 4 | S-006's request turns red: `R-revoke-S-006`. S-009's off-site links go red on their own (default deny). S-010, our planted test page (simulated), comes back FLAGGED by Bedrock Guardrails. S-017 answers 503 twice and becomes a declared hole (planted, simulated) | "Show only blocked and flagged" in Traffic |
| 5 | Extract, then draft and verify. Judges take 3 s each, and the planted malformed verdict is flagged and rerun | Talk through Bobby's gate calls in the log |
| 6 | The run finishes and the brief appears, about 97 s after Approve | Click into the brief, or switch to the finished R2 run (tab 2) as in docs/PLAN.md |

**Stopping a goon, and STOP.** A stopped goon's page has no items, so its drafter calls miss the recording and show as holes. If you stop a goon, press **Stop run**, **Confirm stop** right after it. If the run should finish on stage, skip the goon stop. STOP at any point is safe: in-flight calls come back blocked ("Result discarded"), and the red banner counts the blocked requests.

Rehearsals (fill in with a stopwatch):

| | Launch to approval | Approval to STOP or done | Whole live segment | Notes |
|---|---|---|---|---|
| Rehearsal 1 | | | | |
| Rehearsal 2 | | | | |

If the beats come too fast, ask the build machine to raise `FETCH_PACING`, `VERIFY_PACING` or `STAGE_PACING` (`bash backend/infra/remote.sh --set-env KEY=<seconds>`), and write the new value here.

## 10 minutes before

- [ ] `https://ppgcikvoi0.execute-api.us-east-1.amazonaws.com/healthz` shows `{"ok":true,"version":"0.3.1","mode":"live"}`.
- [ ] The home page lists run_20261108_rzpoj7 and run_20261108_pin2ve.
- [ ] Open the three tabs. Tab 2 on Read.
- [ ] One warm-up run: `axelrod run ...`, approve, STOP at once ($0 under replay_only). It checks the whole path. Then delete it from the home page (Delete on its row, then confirm).
- [ ] The build machine confirms `CACHE_MODELS=replay_only`, `STAGE_PACING=1`, `FETCH_PACING=1.2`, `VERIFY_PACING=3` and `DEMO_FAULTS=fetch,judge` (command above).
- [ ] Notifications off (Windows: Do not disturb). Slack and Discord closed.
- [ ] Charger in. Phone hotspot ready.
- [ ] Browser at 100% zoom, full screen (F11), at least 1600 px wide. The desk is a grid of windows.
- [ ] Terminal: big font, `AXELROD_API`, `PUBLIC_UI_URL` and the `axelrod` function set, `axelrod runs` answers.

## If something breaks

- **Bedrock slow or throttling.** Agent model calls already come from the recording (`replay_only`), so only agent chat depends on Bedrock. If chat is slow, skip it.
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
