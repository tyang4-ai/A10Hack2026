# Axelrod in the terminal

A full-screen terminal version of the dashboard. It talks to the same Axelrod API as the web app, so it works against the mock or the real backend.

```
cd frontend/cli
npm install
./bin/axelrod-tui.js                 # the projects list
./bin/axelrod-tui.js run_20261108_fx0001   # open one run
```

Set `AXELROD_API` to point it somewhere other than `http://localhost:8000/api/v1` (the same variable as the backend's Python CLI). `AXELROD_API_KEY` is sent as `X-Axelrod-Key` when set.

## Keys

| Where | Keys |
|---|---|
| Projects | ↑↓ choose, ⏎ open, n start a run, q quit |
| A run | 1 to 5 or ←→ switch tabs, s twice to stop the run, esc back to projects, q quit |
| Sources (at approval) | a approve, r reject, A approve the rest, ⏎ approve and run |
| Traffic | b show only blocked and flagged |
| Final report | ⏎ show a sentence's chain, ⏎ on a quote to open its source page, esc back |

The tab follows the run's stage until you pick one: sources while planning and approving, traffic while collecting and extracting, agents while verifying, the final report at the end.

## How it fits with the backend CLI

`backend/axelrod/cli.py` is the command-line tool: one command per action (`run`, `tail`, `approve`, `revoke`, `stop`). This is the live view: one screen that follows a run and lets you approve, stop and trace claims down to the source.

## Files

- `src/api.ts`: the API client, typed by `contract/types.ts`.
- `src/useRun.ts`: polls `/events` for traffic and refetches the snapshots when anything changes.
- `src/app.tsx`: the screens.
- `src/index.tsx`: the entry point. It uses the terminal's alternate screen and restores it on exit.

It reuses the web app's formatting (`src/lib/format.ts`), so stage names and money match.
