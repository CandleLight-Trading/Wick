# make dev      -> backend on :8000 and frontend on :5173 (needs GNU make; on Windows without
#                  make, run ./dev.ps1 instead, which does the same thing)
# make test     -> backend unit tests
# make typecheck-> frontend tsc

.PHONY: dev backend frontend test typecheck install

install:
	cd backend && uv sync
	cd frontend && npm install

backend:
	cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8000

frontend:
	cd frontend && npm run dev

dev:
	$(MAKE) -j2 backend frontend

test:
	cd backend && uv run pytest -q

typecheck:
	cd frontend && npm run typecheck
