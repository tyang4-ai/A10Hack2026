# Axelrod

A self-verifying research harness for screening public companies. Built for the SCU ACM AWS x A10 Hack 2026 (A10 Agent Harness and AWS tracks).

Start with [CLAUDE.md](CLAUDE.md), then:
- [docs/CONTRACT.md](docs/CONTRACT.md): the API contract (source of truth: `contract/models.py`)
- [docs/FRONTEND_TLDR.md](docs/FRONTEND_TLDR.md) and [docs/BACKEND.md](docs/BACKEND.md)
- [docs/PLAN.md](docs/PLAN.md): timeline and demo script

## Layout
| Folder | Owner | What |
|---|---|---|
| `contract/` | Gordon proposes, both agree | Pydantic models, generated `types.ts`, BOFFIN rubric |
| `mock/` | Gordon | Mock API with fixtures |
| `backend/` | Gordon | Gateway, pipeline and checks (AWS) |
| `frontend/` | Tong | Web dashboard (Next.js 16 + React 19 + Tailwind 4 + TypeScript) |

## Run the dashboard against the mock
```bash
pip install -r mock/requirements.txt
uvicorn mock.mock_server:app --reload --port 8000
```
```bash
cd frontend
npm install
npm run dev
```
The dashboard calls `NEXT_PUBLIC_API_BASE`, which defaults to `http://localhost:8000/api/v1` (the mock). For the real backend, set it to the API Gateway invoke URL plus `/api/v1`.
