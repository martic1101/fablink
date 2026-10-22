.PHONY: setup dev demo test lint docker

VENV := backend/.venv
PY := $(VENV)/bin/python

setup:            ## install backend + frontend dependencies
	python3 -m venv $(VENV)
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -r backend/requirements-dev.txt
	cd frontend && npm ci

dev:              ## simulator + API (auto-reload) + Vite dev server on :5173
	./scripts/dev.sh

demo:             ## production build served by the API on :8000
	cd frontend && npm run build
	./scripts/dev.sh --demo

test:             ## all backend and frontend tests
	cd backend && .venv/bin/pytest -q
	cd frontend && npm test

lint:
	cd backend && .venv/bin/ruff check . && .venv/bin/ruff format --check .
	cd frontend && npx tsc -b && npm run lint

docker:           ## full stack with PostgreSQL
	docker compose up --build
