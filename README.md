# Axelrod

A harness for AI-written financial market reports: every number traces to an exact quote in an approved source, and every conclusion and forecast is checked before a person sees it. Built for the SCU ACM AWS x A10 Hack 2026 (Agent Harness prompt).

- [CLAUDE.md](CLAUDE.md): start here. Who owns what, branches, contract rules
- [docs/CONTRACT.md](docs/CONTRACT.md): the API contract. Source of truth: [contract/models.py](contract/models.py)
- [docs/PLAN.md](docs/PLAN.md): timeline and demo script
- [docs/BACKEND.md](docs/BACKEND.md) and [docs/FRONTEND_TLDR.md](docs/FRONTEND_TLDR.md): one guide per side
- [docs/DESIGN.md](docs/DESIGN.md) and [docs/API.md](docs/API.md): first drafts, superseded where they differ

## Layout
| Folder | What |
|---|---|
| `frontend/` | Web dashboard (Vite + React + TypeScript), on the `frontend` branch |
| `backend/` | Gateway, pipeline and checks (AWS), on the `backend` branch |
| `contract/` | Shared API contract: Pydantic models, generated TypeScript types, certainty score, BOFFIN rubric |
| `mock/` | Mock API server that serves the whole contract from fixture data |

`main` holds the shared context. `backend` and `frontend` branch off it and get merged back into `main` at the end of the hack.

## Run the mock API
```bash
pip install -r mock/requirements.txt
uvicorn mock.mock_server:app --reload --port 8000   # Swagger UI at http://localhost:8000/docs
```
The fixture company in the mock is fictional.

## Run the dashboard
```bash
cd frontend
npm install
npm run dev
```
Without `VITE_API_URL` set, the dashboard runs against a built-in mock backend that plays through a full run. To use the real backend:
```bash
VITE_API_URL=http://localhost:8000/api/v1 npm run dev                                   # mock API server
VITE_API_URL=https://<api-id>.execute-api.us-east-1.amazonaws.com/api/v1 npm run dev     # real backend
```
