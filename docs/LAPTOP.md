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

Set on EC2 in `/etc/axelrod.env` by the build machine. The laptop never changes them.

| Setting | Value | What it does |
|---|---|---|
| `STAGE_PACING` | 1 (since 4:03 AM, was 3) | A pause of that many seconds at the start of each stage (not at approve). STOP ends it at once |
| `MAX_CONCURRENCY_SMALL`, `MAX_CONCURRENCY_LARGE` | 48, 8 (since 4:03 AM) | More goons in parallel, so a full run aims for about 2:30. A run left going past STOP spends faster |
| `DEMO_FAULTS` | fetch,judge | The planted faults, labeled simulated: S-017 is a broken download (503 twice, then a declared hole), and one judge verdict per run is malformed (flagged, the judge reruns) |
| `CACHE_DOCUMENTS` | on | The gateway decides every fetch, then serves it from the S3 cache |
| `CACHE_MODELS` | replay_only (since about 4:52 AM) | Every agent model call is served from a recorded run (run_20261108_5rzsat, a live recording run, $3.25). Only agent chat calls Bedrock live. A recorded answer that's missing is blocked and becomes a declared hole, never a live call. A full run takes about 63 s from Approve and costs $0. Runs show the CACHED badge and say `cache_mode: documents_and_models` |
| `BUDGET_USD` | 50 | Per run |
| `PROTECTED_RUNS` | rzpoj7, pin2ve | Delete refuses the demo run and its backup |

Check from the build machine: `bash backend/infra/remote.sh 'grep -E "^(STAGE_PACING|DEMO_FAULTS|CACHE_MODELS|BUDGET_USD)=" /etc/axelrod.env'`

## The live segment, click by click

Times are seconds from the moment you click **Approve and run**. Measured with `STAGE_PACING=3` and the old concurrency on the deployed URL (run_20261108_0ntqmj, 3:05 AM, a headless browser with all sources approved). With `STAGE_PACING=1` since 4:03 AM, expect every beat about 2 to 4 seconds earlier (two pauses of 1 s instead of 3 s). The order stays the same. Revoking S-006 right after approval still works, and the first rehearsal gives the real numbers.

**Under `replay_only` (since about 4:52 AM) model answers come back at once, so everything after collect is much faster: a whole run finishes about 63 s after Approve.** STOP before that, or the run completes on its own. If the beats come too fast, ask the build machine to raise `STAGE_PACING` (the user may set about 5). Approval changes (the drag, the reject, the revoke) are being checked against the recording: see the note at the end of this section.

| When | What happens | What you do |
|---|---|---|
| before | `axelrod run ...` prints the link. The plan streams in. Bobby's plan call lands about 10 s after launch, then the approval gate opens | Open the run. At approval: drag S-002 above S-001, reject S-016. Never touch S-006, S-009, S-010 or S-017. Click **Approve and run** |
| 0 s | Collect starts (a short stage pause) | Click **Revoke** on S-006, then **Confirm revoke**, right away. Don't wait for the first fetches |
| 3 s | Fetches start. Each says `cache` and when the copy was collected | Talk through the traffic |
| about 19 s | S-006's request turns red: `R-revoke-S-006` | Point at it |
| about 20 s | S-009's off-site links go red on their own (default deny). S-010, our planted test page (simulated), comes back FLAGGED by Bedrock Guardrails | "Show only blocked and flagged" in Traffic |
| about 21 s | S-017 fails, 503 twice (planted, simulated). It becomes a declared hole | Point at it |
| about 22 s | Extract starts (a short stage pause) | |
| about 27 s | The first extractor calls | Stop one goon: click a working extractor in the Agents window (or Ctrl+K, type its id, Enter), then **Stop agent**, **Confirm stop**. Its next call is blocked, the rest keep going |
| about 35 s | | **Stop run**, **Confirm stop**. The red banner counts the blocked requests |

Rehearsals (fill in with a stopwatch):

| | Launch to approval | Approval to STOP | Whole live segment | Notes |
|---|---|---|---|---|
| Rehearsal 1 | | | | |
| Rehearsal 2 | | | | |

**Approval changes and the recording (backend session, 5:05 AM):** up to a STOP in extract, the demo clicks are safe, because extractor prompts carry page text and no ids. After extract, the drag, the reject and the revoke change the prompts, so draft, judges and the brief would miss the first recording. A second recording that follows the exact demo path (priority sources approved, S-002 above S-001, S-016 rejected, S-006 revoked at the start of collect, no goon stop) is being made, and both recordings replay. **If the demo runs past extract, either skip the goon stop or press STOP right after it**, because a stopped goon's page has no items and its drafter calls would miss and show as holes. New pacing (`FETCH_PACING`, `VERIFY_PACING`) is coming, aiming for a full run of 2:00 to 2:30, and the final numbers go in `backend/status.md`. Update the table above from there.

If there isn't enough time between beats, ask the build machine to raise `STAGE_PACING` (`bash backend/infra/remote.sh --set-env STAGE_PACING=<seconds>`), and write the new value here.

## 10 minutes before

- [ ] `https://ppgcikvoi0.execute-api.us-east-1.amazonaws.com/healthz` shows `{"ok":true,"version":"0.3.1","mode":"live"}`.
- [ ] The home page lists run_20261108_rzpoj7 and run_20261108_pin2ve.
- [ ] Open the three tabs. Tab 2 on Read.
- [ ] One warm-up run: `axelrod run ...`, approve, STOP at once ($0 under replay_only). It checks the whole path. Then delete it from the home page (Delete on its row, then confirm).
- [ ] The build machine confirms `STAGE_PACING=1` and `DEMO_FAULTS=fetch,judge` (command above).
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
