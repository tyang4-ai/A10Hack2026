# Axelrod

A harness for AI-written financial market reports: every number traces to an exact quote in an approved source, and every conclusion and forecast is checked before a person sees it. Built for the SCU ACM AWS x A10 Hack 2026 (Agent Harness prompt).

- [docs/DESIGN.md](docs/DESIGN.md): pipeline, source tiers, checks, evaluation
- [docs/API.md](docs/API.md): dashboard ↔ backend contract

## Layout
| Folder | What |
|---|---|
| `frontend/` | Web dashboard (Vite + React + TypeScript) |
| `backend/` | Gateway, pipeline and checks (AWS) |

## Run the dashboard
```bash
cd frontend
npm install
npm run dev
```
Without `VITE_API_URL` set, the dashboard runs against a built-in mock backend that plays through a full run. To use the real backend:
```bash
VITE_API_URL=https://<api-id>.execute-api.<region>.amazonaws.com/prod npm run dev
```
